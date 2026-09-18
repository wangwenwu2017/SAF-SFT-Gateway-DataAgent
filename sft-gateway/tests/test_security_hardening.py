"""路径、header、认证和序号基础回归。输入为虚构凭据、MockTransport 和临时 DB；输出为行为断言。两个存储连接按顺序调用，不能证明并发慢流的锁行为。"""

from __future__ import annotations

import asyncio
import httpx
import pytest

from sft_gateway.app import _response_headers, _upstream_path, make_app
from sft_gateway.gateway.storage import Storage


def test_upstream_path_rejects_absolute_and_traversal_targets_fastapi() -> None:
    from fastapi import HTTPException

    for value in ("http://attacker.test/v1/responses", "//attacker.test/x", "v1/../secret"):
        with pytest.raises(HTTPException):
            _upstream_path("https://relay.example/v1", value)
    assert _upstream_path("https://relay.example/v1", "/v1/responses") == "responses"


def test_decoded_response_does_not_retain_content_encoding() -> None:
    headers = httpx.Headers({"content-encoding": "gzip", "content-type": "application/json", "x-cache-hit": "true"})
    forwarded = _response_headers(headers)
    assert "content-encoding" not in forwarded
    assert forwarded["x-cache-hit"] == "true"


def test_storage_sequence_is_atomic_across_connections(tmp_path) -> None:
    path = tmp_path / "capture.sqlite"
    first, second = Storage(path), Storage(path)
    try:
        assert first.start_request("a", "run", "responses", "/v1/responses", "m", {}, {}) == 1
        assert second.start_request("b", "run", "responses", "/v1/responses", "m", {}, {}) == 2
    finally:
        first.close()
        second.close()


def test_gateway_api_key_is_separate_from_upstream_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SFT_GATEWAY_API_KEY", "gateway-secret")
    monkeypatch.setenv("SFT_UPSTREAM_API_KEY", "upstream-secret")
    seen: dict[str, str] = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization", "")
        seen["gateway"] = request.headers.get("x-gateway-key", "")
        return httpx.Response(200, json={"id": "r", "status": "completed", "output": []})

    async def run() -> None:
        app = make_app("http://upstream", str(tmp_path / "db.sqlite"), transport=httpx.MockTransport(upstream))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway", headers={"authorization": "Bearer test-billing", "x-admin-key": "test-admin"}) as client:
                body = {"model": "m", "input": "hello"}
                denied = await client.post("/runs/a/v1/responses", json=body)
                assert denied.status_code == 401
                accepted = await client.post("/runs/a/v1/responses", headers={"x-gateway-key": "gateway-secret"}, json=body)
                assert accepted.status_code == 200
            assert seen == {"authorization": "Bearer upstream-secret", "gateway": ""}
        finally:
            app.state.storage.close()

    asyncio.run(run())
