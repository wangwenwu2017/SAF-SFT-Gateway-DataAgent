"""HTTP/会话/SSE 基础回归。输入为临时 DB、内存上游和请求样例；输出为接口、转发和分组断言。测试不等于完整安全或多 worker 验收。"""

from __future__ import annotations

import asyncio
import json

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from sft_gateway.app import make_app
from sft_gateway.gateway.detection import Protocol
from sft_gateway.sse import SSEParser


def test_sse_parser_multiline_utf8() -> None:
    parser = SSEParser()
    events = parser.feed('event: message\r\ndata: {"text":"你好"}\r\n\r\n'.encode())
    assert events[0].json() == {"text": "你好"}


def test_incremental_codex_responses_are_one_trajectory(tmp_path) -> None:
    upstream = FastAPI()

    @upstream.post("/v1/responses")
    async def responses(request: Request):
        body = await request.json()
        previous = body.get("previous_response_id")
        rid = "resp_2" if previous else "resp_1"
        return JSONResponse({"id": rid, "object": "response", "status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": rid}]}]})

    async def run() -> None:
        app = make_app("http://upstream", str(tmp_path / "db.sqlite"), transport=httpx.ASGITransport(app=upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway", headers={"authorization": "Bearer test-billing", "x-admin-key": "test-admin"}) as client:
            first = await client.post("/runs/run-a/v1/responses", headers={"authorization": "Bearer billing-key"}, json={"model": "m", "instructions": "sys", "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "a"}]}]})
            assert first.status_code == 200
            second = await client.post("/runs/run-a/v1/responses", headers={"authorization": "Bearer billing-key"}, json={"model": "m", "instructions": "sys", "previous_response_id": "resp_1", "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "b"}]}]})
            assert second.status_code == 200
            exported = (await client.post("/runs/run-a/export")).json()
            assert exported["stats"] == {"records_total": 2, "trajectories": 1, "samples": 2, "skipped": 0}
            assert len(exported["trajectories"][0]["messages"]) == 5
            assert exported["samples"][1]["previous_response_id"] == "resp_1"
            assert len(exported["sft_samples"]) == 1
            assert set(exported["sft_samples"][0]) == {"task", "tools", "messages"}
    asyncio.run(run())


def test_billing_key_is_forwarded_but_not_persisted(tmp_path) -> None:
    upstream = FastAPI()
    seen: dict[str, str] = {}

    @upstream.post("/v1/responses")
    async def responses(request: Request):
        seen["authorization"] = request.headers.get("authorization", "")
        return JSONResponse({"id": "resp_1", "status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}]})

    async def run() -> None:
        db = tmp_path / "db.sqlite"
        app = make_app("http://upstream", str(db), transport=httpx.ASGITransport(app=upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway", headers={"authorization": "Bearer test-billing", "x-admin-key": "test-admin"}) as client:
            await client.post("/runs/run-a/v1/responses", headers={"authorization": "Bearer secret-billing-key"}, json={"model": "m", "input": "hi"})
            assert seen["authorization"] == "Bearer secret-billing-key"
        assert "secret-billing-key" not in db.read_bytes().decode("utf-8", errors="ignore")
    asyncio.run(run())


def test_billing_key_does_not_define_trajectory(tmp_path) -> None:
    upstream = FastAPI()

    @upstream.post("/v1/responses")
    async def responses(_: Request):
        return JSONResponse({"id": "resp_root", "status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}]})

    async def run() -> None:
        app = make_app("http://upstream", str(tmp_path / "db.sqlite"), transport=httpx.ASGITransport(app=upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway", headers={"authorization": "Bearer test-billing", "x-admin-key": "test-admin"}) as client:
            body = {"model": "m", "input": "same"}
            await client.post("/v1/responses", headers={"authorization": "Bearer same-key"}, json=body)
            await client.post("/v1/responses", headers={"authorization": "Bearer same-key"}, json=body)
            exported = (await client.post("/runs/unassigned/export")).json()
            assert exported["stats"]["trajectories"] == 2
    asyncio.run(run())


def test_native_codex_headers_define_session_without_path_run_id(tmp_path) -> None:
    upstream = FastAPI()

    @upstream.post("/v1/responses")
    async def responses(request: Request):
        body = await request.json()
        previous = body.get("previous_response_id")
        rid = "resp-native-2" if previous else "resp-native-1"
        return JSONResponse({
            "id": rid,
            "status": "completed",
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": rid}]}],
        })

    async def run() -> None:
        app = make_app("http://upstream", str(tmp_path / "db.sqlite"), transport=httpx.ASGITransport(app=upstream))
        headers = {
            "authorization": "Bearer shared-billing-key",
            "thread-id": "codex-thread-a",
            "session-id": "codex-session-a",
            "x-codex-turn-metadata": json.dumps({"thread_id": "codex-thread-a", "session_id": "codex-session-a"}),
        }
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway", headers={"authorization": "Bearer test-billing", "x-admin-key": "test-admin"}) as client:
            first = await client.post("/v1/responses", headers=headers, json={"model": "m", "input": "a"})
            assert first.status_code == 200
            second = await client.post("/v1/responses", headers=headers, json={"model": "m", "previous_response_id": "resp-native-1", "input": "b"})
            assert second.status_code == 200
            exported = (await client.post("/sessions/codex-thread-a/export")).json()
            assert exported["run_id"] == "codex-thread-a"
            assert exported["stats"]["records_total"] == 2
            assert len(exported["native_trajectories"]) == 1
            assert len(exported["native_trajectories"][0]["records"]) == 2
            assert len(exported["sft_samples"]) == 1
    asyncio.run(run())


def test_explicit_path_run_id_overrides_native_codex_thread(tmp_path) -> None:
    upstream = FastAPI()

    @upstream.post("/v1/responses")
    async def responses(_: Request):
        return JSONResponse({"id": "resp-path", "status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}]})

    async def run() -> None:
        app = make_app("http://upstream", str(tmp_path / "db.sqlite"), transport=httpx.ASGITransport(app=upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway", headers={"authorization": "Bearer test-billing", "x-admin-key": "test-admin"}) as client:
            response = await client.post("/runs/path-scope/v1/responses", headers={"thread-id": "native-thread"}, json={"model": "m", "input": "x"})
            assert response.status_code == 200
            path_export = (await client.post("/runs/path-scope/export")).json()
            native_export = (await client.post("/sessions/native-thread/export")).json()
            assert path_export["stats"]["records_total"] == 1
            assert native_export["stats"]["records_total"] == 0
    asyncio.run(run())


def test_unassigned_billing_key_does_not_enable_prefix_inference(tmp_path) -> None:
    upstream = FastAPI()

    @upstream.post("/v1/responses")
    async def responses(_: Request):
        return JSONResponse({"id": "resp", "status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}]})

    async def run() -> None:
        app = make_app("http://upstream", str(tmp_path / "db.sqlite"), transport=httpx.ASGITransport(app=upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway", headers={"authorization": "Bearer test-billing", "x-admin-key": "test-admin"}) as client:
            await client.post("/v1/responses", headers={"authorization": "Bearer same-key"}, json={"model": "m", "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "first"}]}]})
            await client.post("/v1/responses", headers={"authorization": "Bearer same-key"}, json={"model": "m", "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "first"}]}, {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "second"}]}]})
            exported = (await client.post("/runs/unassigned/export")).json()
            assert exported["stats"]["trajectories"] == 2
    asyncio.run(run())


def test_responses_stream_preserves_completed_output_and_tools(tmp_path) -> None:
    upstream = FastAPI()

    @upstream.post("/v1/responses")
    async def responses(_: Request):
        async def stream():
            yield b"event: response.output_item.added\ndata: {\"type\":\"response.output_item.added\",\"output_index\":0,\"item\":{\"type\":\"function_call\",\"call_id\":\"c1\",\"name\":\"shell\"}}\n\n"
            for delta in ('{"cmd":', ' "pytest"}'):
                yield f"event: response.function_call_arguments.delta\ndata: {json.dumps({'type':'response.function_call_arguments.delta','output_index':0,'delta':delta})}\n\n".encode()
            yield b"event: response.completed\ndata: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_tool\",\"status\":\"completed\",\"output\":[{\"type\":\"function_call\",\"call_id\":\"c1\",\"name\":\"shell\",\"arguments\":\"{\\\"cmd\\\": \\\"pytest\\\"}\"}]}}\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream")

    async def run() -> None:
        app = make_app("http://upstream", str(tmp_path / "db.sqlite"), transport=httpx.ASGITransport(app=upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway", headers={"authorization": "Bearer test-billing", "x-admin-key": "test-admin"}) as client:
            response = await client.post("/runs/run-a/v1/responses", json={"model": "m", "input": "run", "stream": True})
            assert response.status_code == 200
            assert "response.completed" in response.text
            exported = (await client.post("/runs/run-a/export")).json()
            assert exported["samples"][0]["target_items"][0]["type"] == "function_call"
    asyncio.run(run())
