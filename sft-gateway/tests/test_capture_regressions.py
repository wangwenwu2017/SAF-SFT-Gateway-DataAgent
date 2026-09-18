"""HTTP regressions for capture fidelity and message-level SFT eligibility.

These tests use a real ASGI gateway and a deterministic upstream. They need no
external service or credentials and assert exported data as well as raw capture.

捕获状态、正文透传、兼容字段和脱敏回归。输入为临时 DB、模拟 JSON/SSE 上游；输出为捕获与导出断言，不请求真实模型。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from sft_gateway.app import _effective_response, make_app


def _message(role: str, text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": role,
        "content": [
            {"type": "output_text" if role == "assistant" else "input_text", "text": text}
        ],
    }


def _response(response_id: str, status: str, text: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "status": status,
        "output": [_message("assistant", text)],
    }
    if status == "incomplete":
        result["incomplete_details"] = {"reason": "max_output_tokens"}
    elif status == "failed":
        result["error"] = {"code": "server_error", "message": "upstream failure"}
    return result


def _frame(event: dict[str, Any]) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode()


def test_completed_sse_is_recovered_after_client_disconnect() -> None:
    """A terminal event remains trainable even if the consumer closes the stream."""
    payload = _response("resp-recovered", "completed", "answer")
    row = {
        "status": "client_disconnected",
        "protocol": "responses",
        "response": None,
        "events": [
            {"event": "response.created", "data": {"type": "response.created", "response": {"id": "resp-recovered", "output": []}}},
            {"event": "response.completed", "data": {"type": "response.completed", "response": payload}},
        ],
    }
    recovered, was_recovered = _effective_response(row)
    assert was_recovered
    assert recovered == payload


@pytest.mark.parametrize("status", ["completed", "incomplete", "failed"])
def test_nonstream_response_status_controls_training_eligibility(tmp_path, status) -> None:
    """HTTP 200 is not evidence that Responses generation completed."""
    upstream = FastAPI()
    payload = _response("resp_status", status, "partial or complete answer")

    @upstream.post("/v1/responses")
    async def respond():
        return JSONResponse(payload)

    async def run() -> None:
        app = make_app("http://upstream", str(tmp_path / "capture.sqlite"), transport=httpx.ASGITransport(app=upstream))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway", headers={"authorization": "Bearer test-billing", "x-admin-key": "test-admin"}) as client:
                response = await client.post("/runs/status-check/v1/responses", json={"model": "test-model", "input": "hello"})
                assert response.status_code == 200
                assert response.json() == payload
                exported = (await client.post("/runs/status-check/export")).json()
                expected_samples = int(status == "completed")
                assert exported["stats"]["samples"] == expected_samples
                assert exported["stats"]["skipped"] == 1 - expected_samples
                records = app.state.storage.records("status-check")
                assert len(records) == 1
                assert records[0]["status"] == status
                assert records[0]["response"] == payload
                assert json.loads(records[0]["raw_body"]) == payload
        finally:
            app.state.storage.close()

    asyncio.run(run())


@pytest.mark.parametrize("terminal", ["completed", "incomplete", "failed", "eof"])
def test_stream_terminal_status_and_raw_capture(tmp_path, terminal) -> None:
    """Only a completed terminal event yields SFT; EOF alone remains partial."""
    upstream = FastAPI()
    created = {"type": "response.created", "response": {"id": "resp_stream", "status": "in_progress", "output": []}}
    output_done = {"type": "response.output_item.done", "output_index": 0, "item": _message("assistant", "你好")}
    frames = [_frame(created), _frame(output_done)]
    if terminal != "eof":
        frames.append(_frame({"type": f"response.{terminal}", "response": _response("resp_stream", terminal, "你好")}))
    wire = b"".join(frames)

    @upstream.post("/v1/responses")
    async def respond():
        async def chunks():
            # Deliberately split both JSON/SSE fields and multibyte UTF-8.
            for index in range(0, len(wire), 7):
                yield wire[index:index + 7]

        return StreamingResponse(chunks(), media_type="text/event-stream")

    async def run() -> None:
        app = make_app("http://upstream", str(tmp_path / "capture.sqlite"), transport=httpx.ASGITransport(app=upstream))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway", headers={"authorization": "Bearer test-billing", "x-admin-key": "test-admin"}) as client:
                response = await client.post("/runs/stream-check/v1/responses", json={"model": "test-model", "input": "hello", "stream": True})
                assert response.status_code == 200
                assert response.content == wire
                exported = (await client.post("/runs/stream-check/export")).json()
                expected_samples = int(terminal == "completed")
                assert exported["stats"]["samples"] == expected_samples
                assert exported["stats"]["skipped"] == 1 - expected_samples
                record, = app.state.storage.records("stream-check")
                assert record["status"] == ("partial" if terminal == "eof" else terminal)
                assert record["raw_body"] == wire
                assert [event["raw"] for event in record["events"]] == frames
        finally:
            app.state.storage.close()

    asyncio.run(run())


def test_full_history_without_response_link_exports_each_message_once(tmp_path) -> None:
    """Codex-style history resend must merge by prefix without duplicate output."""
    upstream = FastAPI()
    requests: list[dict[str, Any]] = []
    answers = [_response("resp_1", "completed", "answer one"), _response("resp_2", "completed", "answer two")]

    @upstream.post("/v1/responses")
    async def respond(request: Request):
        requests.append(await request.json())
        return JSONResponse(answers[len(requests) - 1])

    first_input = [_message("user", "question one")]
    second_input = first_input + answers[0]["output"] + [_message("user", "question two")]
    expected_messages = [
        {"role": "system", "content": "be precise"},
        {"role": "user", "content": "question one"},
        {"role": "assistant", "content": "answer one"},
        {"role": "user", "content": "question two"},
        {"role": "assistant", "content": "answer two"},
    ]

    async def run() -> None:
        app = make_app("http://upstream", str(tmp_path / "capture.sqlite"), transport=httpx.ASGITransport(app=upstream))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway", headers={"authorization": "Bearer test-billing", "x-admin-key": "test-admin"}) as client:
                for context in (first_input, second_input):
                    response = await client.post("/runs/prefix-check/v1/responses", json={"model": "test-model", "instructions": "be precise", "input": context})
                    assert response.status_code == 200
                assert requests[1]["input"] == second_input
                exported = (await client.post("/runs/prefix-check/export")).json()
                assert exported["stats"] == {"records_total": 2, "trajectories": 1, "samples": 2, "skipped": 0}
                assert exported["trajectories"][0]["messages"] == expected_messages
                assert exported["samples"][1]["messages"] == expected_messages
        finally:
            app.state.storage.close()

    asyncio.run(run())


def test_upstream_url_with_v1_preserves_api_path_and_query(tmp_path) -> None:
    """Relay base URLs conventionally already include /v1."""
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.url.query))
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": []})
        if request.url.path == "/v1/responses":
            return httpx.Response(200, json=_response("r", "completed", "ok"))
        return httpx.Response(404)

    async def run() -> None:
        app = make_app("http://upstream/v1", str(tmp_path / "capture.sqlite"), transport=httpx.MockTransport(upstream))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway", headers={"authorization": "Bearer test-billing", "x-admin-key": "test-admin"}) as client:
                assert (await client.get("/runs/base-path/v1/models?probe=1")).status_code == 200
                assert (await client.post("/runs/base-path/v1/responses?probe=2", json={"model": "m", "input": "hello"})).status_code == 200
            assert seen == [("GET", "/v1/models", b"probe=1"), ("POST", "/v1/responses", b"probe=2")]
        finally:
            app.state.storage.close()

    asyncio.run(run())


def test_responses_cache_controls_and_metadata_are_forwarded_unchanged(tmp_path) -> None:
    """The relay must preserve provider prompt-cache hints and hit metadata."""
    seen: dict[str, Any] = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        seen["raw"] = request.content
        seen["body"] = json.loads(request.content)
        seen["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "cache-control": "private, max-age=0",
                "x-cache-hit": "true",
                "x-openai-cache-hit": "1",
            },
            json=_response("cache-response", "completed", "cached"),
        )

    async def run() -> None:
        app = make_app("http://upstream", str(tmp_path / "capture.sqlite"), transport=httpx.MockTransport(upstream))
        try:
            body = {
                "model": "gpt-6-astra",
                "instructions": "stable system prompt",
                "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "next"}]}],
                "previous_response_id": "cache-response-parent",
                "prompt_cache_key": "task-cache-key",
                "prompt_cache_retention": "24h",
                "stream": False,
            }
            raw_body = (
                b'{\n  "model": "gpt-6-astra", "instructions": "stable system prompt", '
                b'"input": [{"type": "message", "role": "user", "content": '
                b'[{"type": "input_text", "text": "next"}]}], '
                b'"previous_response_id": "cache-response-parent", '
                b'"prompt_cache_key": "task-cache-key", "prompt_cache_retention": "24h", '
                b'"stream": false\n}'
            )
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway", headers={"authorization": "Bearer test-billing", "x-admin-key": "test-admin"}) as client:
                response = await client.post(
                    "/runs/cache-check/v1/responses",
                    headers={"x-client-trace": "trace-1", "cache-control": "no-cache", "content-type": "application/json"},
                    content=raw_body,
                )
                assert response.status_code == 200
                assert response.headers["x-cache-hit"] == "true"
                assert response.headers["x-openai-cache-hit"] == "1"
                assert response.headers["cache-control"] == "private, max-age=0"
            assert seen["raw"] == raw_body
            assert seen["body"] == body
            assert seen["headers"]["x-client-trace"] == "trace-1"
            assert seen["headers"]["cache-control"] == "no-cache"
        finally:
            app.state.storage.close()

    asyncio.run(run())


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_codex_metadata_compatibility_preserves_capture(tmp_path, monkeypatch, stream, enabled) -> None:
    """A relay compatibility shim must not alter stored context or cache hints."""
    monkeypatch.setenv("SFT_STRIP_CODEX_INTERNAL_METADATA", str(int(enabled)))
    item = _message("user", "hello")
    item["internal_chat_message_metadata_passthrough"] = {"content_item_kinds": ["user.text"], "turn_id": "turn-a"}
    body = {"model": "gpt-6-astra", "input": [item], "stream": stream, "prompt_cache_key": "stable-key", "client_metadata": {"trace": "x"}}
    raw = json.dumps(body, indent=2).encode()
    expected = json.loads(raw)
    if enabled:
        del expected["input"][0]["internal_chat_message_metadata_passthrough"]

    def upstream(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == expected
        if not enabled:
            assert request.content == raw
        response = _response("compat-response", "completed", "ok")
        if stream:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_frame({"type": "response.completed", "response": response}))
        return httpx.Response(200, json=response)

    async def run() -> None:
        app = make_app("http://upstream", str(tmp_path / "capture.sqlite"), transport=httpx.MockTransport(upstream))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway", headers={"authorization": "Bearer test-billing", "x-admin-key": "test-admin"}) as client:
                response = await client.post("/runs/compat-check/v1/responses", content=raw, headers={"content-type": "application/json"})
                assert response.status_code == 200
            record, = app.state.storage.records("compat-check")
            assert record["request"] == body
            assert record["status"] == "completed"
        finally:
            app.state.storage.close()

    asyncio.run(run())


@pytest.mark.parametrize("configured", [False, True])
def test_billing_credentials_absent_from_live_wal_records_and_exports(tmp_path, monkeypatch, configured) -> None:
    """Inspect live WAL as well as the DB; an uncheckpointed DB alone is insufficient."""
    incoming_key = "incoming-private-billing-key-4e9d"
    configured_key = "configured-private-billing-key-6b2a"
    auxiliary_key = "auxiliary-private-key-a18d"
    if configured:
        monkeypatch.setenv("SFT_UPSTREAM_API_KEY", configured_key)
        monkeypatch.setenv("SFT_GATEWAY_API_KEY", incoming_key)
    else:
        monkeypatch.delenv("SFT_UPSTREAM_API_KEY", raising=False)
    upstream = FastAPI()
    received_headers: dict[str, str] = {}

    @upstream.post("/v1/responses")
    async def respond(request: Request):
        received_headers.update(request.headers)
        return JSONResponse(_response("resp_secret", "completed", "ok"))

    async def run() -> None:
        db_path = tmp_path / "capture.sqlite"
        app = make_app("http://upstream", str(db_path), transport=httpx.ASGITransport(app=upstream))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway", headers={"authorization": "Bearer test-billing", "x-admin-key": "test-admin"}) as client:
                response = await client.post("/runs/secret-check/v1/responses", headers={"Authorization": f"Bearer {incoming_key}", "X-Api-Key": auxiliary_key, "X-Goog-Api-Key": auxiliary_key}, json={"model": "test-model", "input": "hello"})
                assert response.status_code == 200
                assert received_headers["authorization"] == f"Bearer {configured_key if configured else incoming_key}"
                assert "x-api-key" not in received_headers
                assert "x-goog-api-key" not in received_headers
                exported = await client.post("/runs/secret-check/export")
                records = app.state.storage.records("secret-check")
                # The connection is deliberately still open while inspecting WAL.
                wal_path = tmp_path / "capture.sqlite-wal"
                assert wal_path.exists()
                persisted_files = [db_path, wal_path]
                persisted = b"".join(path.read_bytes() for path in persisted_files)
                captured = repr(records).encode() + exported.content
                for secret in (incoming_key, configured_key, auxiliary_key):
                    assert secret.encode() not in persisted
                    assert secret.encode() not in captured
        finally:
            app.state.storage.close()

    asyncio.run(run())
