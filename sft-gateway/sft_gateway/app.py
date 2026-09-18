"""FastAPI 转发与采集入口。输入原生 HTTP 请求，输出上游响应和 SQLite 捕获。
模块导入无数据库写入；默认 ASGI app 在第一次调用时初始化。固定计费 key 需要
独立 gateway key，管理 key 缺省时禁用管理 API；此服务不实现多租户所有权模型。"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any, AsyncIterator, Mapping
from urllib.parse import urlsplit

import httpx
import anyio
from functools import partial
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from .config import GatewayConfig
from .export.messages import export_samples, export_trajectories
from .export.codex import export_codex_samples
from .export.validation import validate_trajectory_tools
from .gateway.detection import Protocol, detect_protocol
from .gateway.session import RunRegistry, extract_auth, extract_native_session_id, redacted_headers, resolve_run_id, validate_run_id
from .gateway.storage import Storage
from .protocols.chat import ChatAdapter
from .protocols.responses import ResponsesAdapter
from .protocols.responses_state import ResponsesStreamState
from .protocols.compatibility import responses_wire_body
from .sse import SSEParser
from .trajectory.context_fingerprint import context_fingerprint
from .trajectory.conversation_index import ConversationIndex
from .trajectory.models import CompletedCompletion


def _json(raw: bytes) -> dict[str, Any]:
    """把 HTTP 正文解析为 JSON 对象。

    输入：原始 bytes；输出：dict，空正文返回空对象。非对象/非法 JSON 抛 HTTP 400。
    副作用：无；这里不验证具体模型协议的字段类型或正文大小。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="request body must be a JSON object") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    return value


def _adapter(protocol: Protocol) -> Any:
    """按协议返回新建的无状态协议适配器。

    输入：Protocol；输出：ResponsesAdapter 或 ChatAdapter。无 I/O；非 Responses
    值会落入 Chat 分支，调用方应先通过协议检测。"""
    return ResponsesAdapter() if protocol == Protocol.RESPONSES else ChatAdapter()


def _validate_upstream_base_url(base_url: str) -> None:
    """检查配置上游 URL 的基本结构。

    输入：base_url 字符串；输出：None，非法结构抛 ValueError。禁止非 HTTP(S)、
    缺少 authority、userinfo、query 和 fragment；不访问网络，也不解析 DNS。
    这只是配置校验，不能代替转发时对最终目标 origin 的检查。"""
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("SFT_UPSTREAM_URL must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("SFT_UPSTREAM_URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("SFT_UPSTREAM_URL must not contain a query or fragment")


async def _storage_call(function: Any, *args: Any, **kwargs: Any) -> Any:
    """在线程中完成一个持久化操作；取消期间等待已提交任务结束，返回其结果。"""
    with anyio.CancelScope(shield=True):
        return await anyio.to_thread.run_sync(partial(function, *args, **kwargs))


class _ConcurrencyLimit:
    """ASGI 请求限流；输入 app/上限，流响应结束才释放槽，超额返回 503。"""
    def __init__(self, app: Any, limit: int) -> None:
        """保存下游 ASGI app 和进程内活跃请求上限，无 I/O。"""
        self.app, self.limit, self.active = app, limit, 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """转发完整 ASGI 生命周期；拒绝超额 HTTP 请求，不分配无界等待队列。"""
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if self.active >= self.limit:
            return await Response("gateway concurrency limit reached", status_code=503)(scope, receive, send)
        self.active += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.active -= 1


async def _bounded_body(response: httpx.Response, limit: int) -> bytes:
    """读取已解压响应 bytes，超过 limit 抛 ValueError；调用者负责关闭连接。"""
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > limit:
            raise ValueError("upstream response exceeds configured limit")
        body.extend(chunk)
    return bytes(body)


def make_app(upstream: str | None = None, db_path: str | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """构造 FastAPI。输入可选 upstream、db_path 和测试 transport，输出 ASGI 应用。
    显式调用会打开/初始化数据库；lifespan 退出关闭连接。固定上游凭据须配置
    gateway key；大小及并发上限来自环境变量，未配置管理 key 时禁用管理 API。"""
    config = GatewayConfig.from_env(upstream=upstream, db=db_path)
    _validate_upstream_base_url(config.upstream_base_url)
    if config.upstream_api_key and not config.gateway_api_key:
        raise ValueError("SFT_GATEWAY_API_KEY is required with a fixed upstream billing key")
    storage = Storage(config.database_path, raw_archive_path=config.raw_archive_path)
    registry = RunRegistry()
    instance_id = config.instance_id or secrets.token_hex(12)
    if min(config.max_request_bytes, config.max_response_bytes, config.max_stream_bytes, config.max_frame_bytes, config.max_concurrent_requests) <= 0:
        storage.close()
        raise ValueError("capture limits must be positive")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        """管理本 app 的正常 ASGI 生命周期。

        输入：FastAPI 实例（无需使用）；输出：异步上下文的空 yield。退出时关闭
        SQLite；不是按请求的清理钩子，不负责导出文件或补齐中断模型响应。"""
        yield
        await _storage_call(storage.close, )

    app = FastAPI(title="SFT Gateway", version="0.3.2", lifespan=lifespan)
    app.state.storage = storage
    app.add_middleware(_ConcurrencyLimit, limit=config.max_concurrent_requests)

    def _validated_id(value: Any) -> str:
        """为显式管理接口校验标识符。

        输入：任意值；输出：规范化字符串。非字符串或非法格式转成 HTTP 400；无 I/O。
        模型代理中的 resolve_run_id 当前并不经过此包装。"""
        if not isinstance(value, str):
            raise HTTPException(status_code=400, detail="identifier must be a string")
        try:
            return validate_run_id(value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/")
    async def root() -> dict[str, str]:
        """返回服务标识。

        输入：无；输出：固定状态字典；无 I/O，也不校验数据库或上游可用性。"""
        return {"status": "ok", "service": "sft-gateway"}

    @app.get("/health")
    async def health() -> dict[str, str]:
        """返回进程存活状态和配置的上游地址。

        输入：无；输出：状态及 upstream 字典。无认证、无外部探测；200 不代表上游
        模型可用；调用者可对比 instance_id 确认当前启动实例。"""
        return {"status": "ok", "upstream": config.upstream_base_url, "instance_id": instance_id}

    @app.post("/runs")
    async def create_run(request: Request) -> dict[str, str]:
        """登记一个显式采集 run。

        输入：HTTP JSON，可含 run_id；输出：run_id 字典。先执行管理认证，缺 ID 时
        生成随机值；只写进程内 registry，不创建持久化请求记录。"""
        _require_admin(request)
        body = _json(await request.body())
        run_id = _validated_id(body.get("run_id") or f"run_{secrets.token_hex(8)}")
        registry.ensure(run_id, explicit=True)
        return {"run_id": run_id}

    @app.get("/runs/{run_id}")
    async def get_run(request: Request, run_id: str) -> dict[str, Any]:
        """查询指定 run 的采集数量。

        输入：请求认证头和路径 run_id；输出：ID、记录数、exists。读取 registry/
        SQLite；空 run 的进程内登记不会跨进程共享，不提供用户级所有权检查。"""
        _require_admin(request)
        run_id = _validated_id(run_id)
        rows = await asyncio.to_thread(storage.records, run_id)
        return {"run_id": run_id, "completion_count": len(rows), "exists": run_id in registry.runs or bool(rows)}

    @app.get("/runs/{run_id}/completions")
    async def list_completions(request: Request, run_id: str) -> dict[str, Any]:
        """管理接口读取一个 run 的捕获。输入认证请求/run_id，输出记录数组。
        数据库在线程中读取，bytes 用 base64 表示；无管理 key 时返回 403。"""
        _require_admin(request)
        run_id = _validated_id(run_id)
        return {"run_id": run_id, "completions": [_json_record(row) for row in await asyncio.to_thread(storage.records, run_id)]}

    @app.post("/runs/{run_id}/export")
    async def export_run(request: Request, run_id: str) -> dict[str, Any]:
        """输入管理认证请求和 run_id，在线程中读取快照及重建轨迹，输出导出 JSON。
        不写文件；资格仅表示该快照可投影为 SFT，不表示任务结束。"""
        _require_admin(request)
        run_id = _validated_id(run_id)
        return await asyncio.to_thread(_export_payload, run_id, await asyncio.to_thread(storage.records, run_id))

    @app.post("/sessions/{session_id}/export")
    async def export_session(request: Request, session_id: str) -> dict[str, Any]:
        """按原生会话 ID 查询对应采集范围并在线导出。

        输入：认证请求及 session_id；输出：与 run 导出相同的 JSON。这里直接把
        session_id 当存储 run_id 查询；不会搜索其他显式 run 中的同名 native session。"""
        _require_admin(request)
        session_id = _validated_id(session_id)
        return await asyncio.to_thread(_export_payload, session_id, await asyncio.to_thread(storage.records, session_id))

    @app.api_route("/runs/{run_id}/{path:path}", methods=["POST", "GET"])
    async def run_proxy(request: Request, run_id: str, path: str):
        """将带显式 run 前缀的模型请求分派给转发器。

        输入：HTTP 请求、run_id、剩余路径；输出：模型列表或模型响应。先检查可选的
        代理 key，模型采集会写数据库；不检查调用者是否拥有给定 run。"""
        _require_proxy(request)
        if path.rstrip("/") == "v1/models" and request.method == "GET":
            return await forward_models(request, path)
        return await proxy(request, path, run_id=run_id)

    @app.api_route("/{path:path}", methods=["POST", "GET"])
    async def catch_all(request: Request, path: str):
        """处理原生 /v1 等无显式 run 的代理请求。

        输入：请求及捕获路径；输出：模型列表或模型响应。检查可选代理认证后根据
        路径分派；会话作用域由请求头等元数据决定，不能从共享计费 key 推断任务。"""
        _require_proxy(request)
        if path.rstrip("/") == "v1/models" and request.method == "GET":
            return await forward_models(request, path)
        return await proxy(request, path, run_id=None)

    async def forward_models(request: Request, path: str) -> Response:
        """转发 GET 模型列表请求。

        输入：HTTP 请求及路径；输出：上游状态、解压后的正文及过滤后的 headers。
        访问上游但不保存模型列表采集；认证选择与模型转发一致，查询串原样追加。"""
        headers = _forward_request_headers(request.headers)
        auth = extract_auth(request.headers, config.upstream_api_key)
        headers.pop("authorization", None)
        headers.pop("x-api-key", None)
        if config.gateway_api_key and not config.upstream_api_key and _authorization_matches_gateway(request, config.gateway_api_key):
            auth = None
        if auth:
            headers["authorization"] = auth
        kwargs: dict[str, Any] = {"timeout": httpx.Timeout(30.0, connect=10.0)}
        if transport is not None:
            kwargs["transport"] = transport
        upstream_path = _upstream_path(config.upstream_base_url, path)
        target = f"{upstream_path}?{request.url.query}" if request.url.query else upstream_path
        async with httpx.AsyncClient(base_url=config.upstream_base_url, **kwargs) as client:
            async with client.stream("GET", target, headers=headers) as response:
                raw = await _bounded_body(response, config.max_response_bytes)
        return Response(raw, response.status_code, headers=_response_headers(response.headers))

    def _require_admin(request: Request) -> None:
        """验证管理凭据；输入 Request，输出 None 或 HTTP 401/403。
        接受 x-admin-key 或 Bearer；未配置管理 key 时禁用接口，不执行网络 I/O。"""
        if not config.admin_api_key:
            raise HTTPException(status_code=403, detail="management API disabled")
        provided = request.headers.get("x-admin-key") or request.headers.get("authorization", "")
        if provided.lower().startswith("bearer "):
            provided = provided[7:]
        if not secrets.compare_digest(provided, config.admin_api_key):
            raise HTTPException(status_code=401, detail="admin authentication required")

    def _require_proxy(request: Request) -> None:
        """验证模型入口凭据；输入 Request，输出 None 或 HTTP 401。
        固定上游模式使用独立 gateway key；透传模式要求入站上游认证。共享凭据不表示会话身份。"""
        if not config.gateway_api_key:
            if not extract_auth(request.headers, None):
                raise HTTPException(status_code=401, detail="upstream authentication required")
            return
        provided = request.headers.get("x-gateway-key") or request.headers.get("authorization", "")
        if provided.lower().startswith("bearer "):
            provided = provided[7:]
        if not secrets.compare_digest(provided, config.gateway_api_key):
            raise HTTPException(status_code=401, detail="gateway authentication required")
        if not config.upstream_api_key and (not extract_auth(request.headers, None) or _authorization_matches_gateway(request, config.gateway_api_key)):
            raise HTTPException(status_code=401, detail="separate upstream credential required in passthrough mode")

    def _authorization_matches_gateway(request: Request, gateway_key: str) -> bool:
        """判断 Bearer 头是否恰好使用本地 gateway key。

        输入：请求和已配置 gateway_key；输出：bool。无 I/O；用于阻止这一 Bearer
        凭据在缺少固定上游 key 时被错误当作上游认证。"""
        provided = request.headers.get("authorization", "")
        return provided.lower().startswith("bearer ") and secrets.compare_digest(provided[7:], gateway_key)

    async def proxy(request: Request, path: str, *, run_id: str | None) -> Response:
        """转发并采集一次 POST 模型请求；输入 Request、端点和可选 run_id，输出 HTTP 响应。
        限定正文大小，保存接收/转发 bytes，认证头先过滤，持久化操作在线程中等待完成。
        不会自动重试计费请求；请求完成不代表 harness 任务完成。"""
        if request.method != "POST":
            raise HTTPException(status_code=405, detail="only POST model requests are supported")
        # Keep the original bytes as well as the parsed object.  Re-encoding a
        # JSON object is semantically equivalent, but an upstream cache or
        # signing layer may key on the canonical wire payload.  Forwarding the
        # bytes avoids changing whitespace, key order, or number formatting.
        chunks = bytearray()
        async for chunk in request.stream():
            if len(chunks) + len(chunk) > config.max_request_bytes:
                raise HTTPException(status_code=413, detail="request exceeds configured limit")
            chunks.extend(chunk)
        raw_request_body = bytes(chunks)
        body = _json(raw_request_body)
        try:
            protocol = detect_protocol(path, request.headers, body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        path_run_id = run_id
        upstream_path = _upstream_path(config.upstream_base_url, path)
        target = f"{upstream_path}?{request.url.query}" if request.url.query else upstream_path
        resolved_run = resolve_run_id(request.headers, request.query_params, body, registry, path_run_id=path_run_id)
        request_id = f"req_{secrets.token_hex(12)}"
        model = body.get("model") if isinstance(body.get("model"), str) else None
        await _storage_call(storage.start_request, request_id, resolved_run, protocol.value, request.url.path, model, body, redacted_headers(request.headers))
        # Capture always retains the original input. Some relays reject Codex's
        # internal message annotations; the opt-in shim only changes outbound
        # Responses input items, leaving prompts and cache controls intact.
        wire_body = responses_wire_body(body, raw_request_body) if protocol == Protocol.RESPONSES and config.strip_codex_internal_metadata else raw_request_body
        await _storage_call(storage.save_request_wire, request_id, raw_request_body, wire_body)
        headers = _forward_request_headers(request.headers)
        auth = extract_auth(request.headers, config.upstream_api_key)
        if config.gateway_api_key and not config.upstream_api_key and _authorization_matches_gateway(request, config.gateway_api_key):
            auth = None
        if auth:
            headers["authorization"] = auth
        kwargs: dict[str, Any] = {"timeout": httpx.Timeout(config.request_timeout_seconds, connect=30.0)}
        if transport is not None:
            kwargs["transport"] = transport
        if body.get("stream") is True:
            return await _stream(request_id, resolved_run, protocol, target, body, wire_body, headers, kwargs)
        try:
            async with httpx.AsyncClient(base_url=config.upstream_base_url, **kwargs) as client:
                async with client.stream("POST", target, content=wire_body, headers=headers) as response:
                    raw = await _bounded_body(response, config.max_response_bytes)
        except (httpx.HTTPError, ValueError) as exc:
            await _storage_call(storage.finish, request_id, status="upstream_error", status_code=None, response=None, raw_body=None, response_id=None, previous_response_id=_adapter(protocol).previous_response_id(body), error=str(exc))
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        parsed = None
        try:
            value = json.loads(raw)
            parsed = value if isinstance(value, dict) else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        adapter = _adapter(protocol)
        rid = adapter.response_id(parsed)
        response_status = parsed.get("status") if protocol == Protocol.RESPONSES and isinstance(parsed, dict) else None
        if response.is_success and parsed is not None and protocol == Protocol.RESPONSES:
            status = response_status if isinstance(response_status, str) and response_status in {"completed", "incomplete", "failed"} else "incomplete"
        elif response.is_success and parsed is not None and protocol == Protocol.CHAT:
            finish_reason = _chat_finish_reason(parsed)
            status = _chat_finish_status(finish_reason) or "incomplete"
        else:
            status = "completed" if response.is_success and parsed is not None else "upstream_error"
        await _storage_call(storage.finish, request_id, status=status, status_code=response.status_code, response=parsed, raw_body=raw, response_id=rid, previous_response_id=adapter.previous_response_id(body), error=None if response.is_success else raw.decode(errors="replace"))
        return Response(raw, response.status_code, headers=_response_headers(response.headers))

    async def _stream(request_id: str, run_id: str, protocol: Protocol, path: str, body: dict[str, Any], raw_body: bytes, headers: dict[str, str], kwargs: dict[str, Any]) -> Response:
        """打开 SSE 上游并返回 StreamingResponse；输入请求标识、协议、正文、headers 和客户端配置。
        每个事件先持久化再向下游转发，限定帧和总字节数；错误响应按非流式上限读取。
        异常或取消会保留不完整状态，关闭连接；原始流仅指 HTTPX 解压后的正文。"""
        client = httpx.AsyncClient(base_url=config.upstream_base_url, **kwargs)
        target = path
        try:
            upstream_response = await client.send(client.build_request("POST", target, content=raw_body, headers=headers), stream=True)
        except (httpx.HTTPError, ValueError) as exc:
            await client.aclose()
            await _storage_call(storage.finish, request_id, status="upstream_error", status_code=None, response=None, raw_body=None, response_id=None, previous_response_id=_adapter(protocol).previous_response_id(body), error=str(exc))
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if upstream_response.status_code >= 400:
            raw = b""
            try:
                raw = await _bounded_body(upstream_response, config.max_response_bytes)
            except (httpx.HTTPError, ValueError) as exc:
                await _storage_call(storage.finish, request_id, status="upstream_error", status_code=upstream_response.status_code, response=None, raw_body=None, response_id=None, previous_response_id=_adapter(protocol).previous_response_id(body), error=str(exc))
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream_response.aclose()
                    await client.aclose()
            await _storage_call(storage.finish, request_id, status="upstream_error", status_code=upstream_response.status_code, response=None, raw_body=raw, response_id=None, previous_response_id=_adapter(protocol).previous_response_id(body), error=raw.decode(errors="replace"))
            return Response(raw, upstream_response.status_code, headers=_response_headers(upstream_response.headers))

        async def iterator() -> AsyncIterator[bytes]:
            """按上游顺序输出 bytes 并保存 SSE 证据。
            输入来自闭包连接，输出异步 bytes 流；DONE 不升级终态，冲突终态拒绝训练。
            取消期间持久化和连接关闭受到保护，不保证强制杀进程能保存最后尚未接收的内容。"""
            parser = SSEParser(config.max_frame_bytes); state = ResponsesStreamState() if protocol == Protocol.RESPONSES else None; chat_chunks: list[dict[str, Any]] = []; raw_parts: list[bytes] = []; count = 0; terminal = "partial"; total_bytes = 0
            decode_error: str | None = None

            def consume_event(data: Any, done: bool) -> None:
                """输入已存储的事件，更新语义状态；解析错误标记失败但不阻断原始转发。"""
                nonlocal terminal, decode_error
                if decode_error is not None:
                    return
                try:
                    terminal = _event_terminal(protocol, terminal, data, done=done)
                    if state is not None:
                        state.consume(data)
                    elif isinstance(data, dict):
                        chat_chunks.append(data)
                except (ValueError, TypeError, IndexError, KeyError, AttributeError, OverflowError):
                    terminal, decode_error = "failed", "invalid_response_events"

            def snapshot_response() -> dict[str, Any] | None:
                """从当前流状态生成响应快照。

                输入：闭包中的 Responses state 或 Chat chunks；输出：响应 dict 或 None。
                不写数据库、不访问网络；结果受已收到的事件和对应快照实现的字段覆盖限制。"""
                nonlocal terminal, decode_error
                if decode_error is not None:
                    return None
                try:
                    snapshot = state.finalize() if state is not None else None
                    return snapshot.response if snapshot else _chat_snapshot(chat_chunks)
                except (ValueError, TypeError, IndexError, KeyError, AttributeError, OverflowError):
                    terminal, decode_error = "failed", "invalid_response_events"
                    return None

            try:
                async for chunk in upstream_response.aiter_bytes():
                    total_bytes += len(chunk)
                    if total_bytes > config.max_stream_bytes:
                        raise ValueError("stream exceeds configured limit")
                    raw_parts.append(chunk)
                    for event in parser.feed(chunk):
                        data = event.json()
                        if isinstance(data, dict) and event.event and not data.get("type"):
                            data = {**data, "type": event.event}
                        await _storage_call(storage.add_event, request_id, count, event.event, data, event.raw); count += 1
                        consume_event(data, event.data.strip() == "[DONE]")
                    yield chunk
                for event in parser.finish():
                    data = event.json()
                    if isinstance(data, dict) and event.event and not data.get("type"):
                        data = {**data, "type": event.event}
                    await _storage_call(storage.add_event, request_id, count, event.event, data, event.raw); count += 1
                    consume_event(data, event.data.strip() == "[DONE]")
                response = snapshot_response()
                rid = _adapter(protocol).response_id(response)
                await _storage_call(storage.finish, request_id, status=terminal, status_code=upstream_response.status_code, response=response, raw_body=b"".join(raw_parts), response_id=rid, previous_response_id=_adapter(protocol).previous_response_id(body), error=decode_error)
            except (asyncio.CancelledError, GeneratorExit):
                # A harness may close the response after receiving the terminal
                # SSE event.  Preserve that completed snapshot instead of
                # replacing it with an ineligible client_disconnected record.
                if terminal in {"completed", "incomplete", "failed"}:
                    response = snapshot_response()
                    rid = _adapter(protocol).response_id(response)
                    await _storage_call(storage.finish, request_id, status=terminal, status_code=upstream_response.status_code, response=response, raw_body=b"".join(raw_parts), response_id=rid, previous_response_id=_adapter(protocol).previous_response_id(body), error=decode_error)
                else:
                    await _storage_call(storage.finish, request_id, status="client_disconnected", status_code=None, response=None, raw_body=b"".join(raw_parts), response_id=None, previous_response_id=_adapter(protocol).previous_response_id(body), error="client disconnected")
                raise
            except Exception as exc:
                await _storage_call(storage.finish, request_id, status="partial", status_code=None, response=None, raw_body=b"".join(raw_parts), response_id=None, previous_response_id=_adapter(protocol).previous_response_id(body), error=str(exc))
                raise
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream_response.aclose()
                    await client.aclose()
        return StreamingResponse(iterator(), status_code=upstream_response.status_code, headers=_response_headers(upstream_response.headers), media_type=upstream_response.headers.get("content-type", "text/event-stream"))

    return app


def _export_payload(scope_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """把采集记录组装为在线导出 JSON。

    输入：scope_id 和存储记录列表；输出：轨迹、原生记录、两种投影及统计字典。
    仅内存计算；原生记录 bytes 转 base64。这里的 samples 与 sft_samples 是不同
    用途的投影，不能假设都经过相同的 SFT 准入过滤。"""
    trajectories, stats = _build_export(rows)
    trajectory_rows = list(export_trajectories(trajectories))
    sample_rows = list(export_samples(trajectories))
    sft_rows = list(export_codex_samples(trajectories))
    by_request = {row["request_id"]: row for row in rows}
    native_rows = []
    for trajectory in trajectories:
        source_ids = [turn.request_id for turn in trajectory.turns]
        native_rows.append({
            "trajectory_id": trajectory.trajectory_id,
            "run_id": trajectory.run_id,
            "protocol": trajectory.protocol.value,
            "model": trajectory.model,
            "break_reason": trajectory.break_reason,
            "source_request_ids": source_ids,
            "records": [_json_record(by_request[request_id]) for request_id in source_ids if request_id in by_request],
        })
    return {
        "run_id": scope_id,
        "trajectories": trajectory_rows,
        "native_trajectories": native_rows,
        "samples": sample_rows,
        "sft_samples": sft_rows,
        "stats": stats,
    }


def _record_shape_issue(row: dict[str, Any]) -> str | None:
    """校验协议树中的容器和判别字段；返回稳定错误原因，防止单条污染整批导出。"""
    request, response = row.get("request"), row.get("response")
    if not isinstance(request, dict):
        return "invalid_request_object"
    protocol = row.get("protocol")
    if not isinstance(protocol, str) or protocol not in {"responses", "chat"}:
        return "unsupported_protocol"
    if response is not None and not isinstance(response, dict):
        return "invalid_response_object"
    events = row.get("events") or []
    if not isinstance(events, list):
        return "invalid_events"
    stack = [request] + ([response] if response else [])
    if protocol == "responses":
        # These are protocol items; do not descend into tool arguments or
        # arbitrary JSON returned by a tool as if it were a protocol object.
        for field in ("input", "instructions"):
            value = request.get(field)
            if isinstance(value, dict):
                stack.append(value)
            elif isinstance(value, list):
                stack.extend(item for item in value if isinstance(item, dict))
    for event in events:
        if not isinstance(event, dict) or event.get("data") is not None and not isinstance(event["data"], dict):
            return "invalid_event"
        if isinstance(event.get("data"), dict):
            stack.append(event["data"])
    # Descend through protocol-owned structures only, not arbitrary user JSON.
    while stack:
        obj = stack.pop()
        for field in ("content", "summary"):
            parts = obj.get(field)
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, dict) and "type" in part and not isinstance(part["type"], str):
                        return "invalid_content_part_type"
        for key in ("type", "role", "status", "finish_reason"):
            if key in obj and obj[key] is not None and not isinstance(obj[key], str):
                return "invalid_" + key
        for key in ("output", "choices", "tool_calls", "messages"):
            if key not in obj:
                continue
            value = obj[key]
            # A tool result output is text or content parts, not response.output.
            if key == "output" and obj.get("type") in {"function_call_output", "custom_tool_call_output"}:
                continue
            if value is None and key == "tool_calls":
                continue
            if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                return "invalid_" + key
            stack.extend(value)
        for key in ("message", "response", "function"):
            if key in obj and obj[key] is not None:
                if not isinstance(obj[key], dict):
                    return "invalid_" + key
                stack.append(obj[key])
        # Responses argument/text deltas are protocol scalar fragments. They
        # are not message containers and must not be rejected as malformed
        # merely because they are strings.
        if "delta" in obj and obj["delta"] is not None and not isinstance(obj["delta"], (str, dict)):
            return "invalid_delta"
        if isinstance(obj.get("delta"), dict):
            stack.append(obj["delta"])
    if protocol == "responses":
        value = request.get("input", [])
        if not isinstance(value, (str, dict, list)) or isinstance(value, list) and any(not isinstance(item, (str, dict)) for item in value):
            return "invalid_input_items"
        inputs = [value] if isinstance(value, dict) else value if isinstance(value, list) else []
        for item in inputs:
            if isinstance(item, dict) and any(item.get(key) is not None and not isinstance(item[key], str) for key in ("type", "role")):
                return "invalid_input_discriminator"
        if response is not None and not isinstance(response.get("output"), list):
            return "invalid_output_items"
    elif not isinstance(request.get("messages"), list):
        return "invalid_messages"
    elif response is not None and (not isinstance(response.get("choices"), list) or any(not isinstance(c.get("message"), dict) for c in response["choices"])):
        return "invalid_choices"
    return None


def _build_export(rows: list[dict[str, Any]]) -> tuple[list[Any], dict[str, Any]]:
    # Requests without an explicit run/session are intentionally isolated for
    # prefix inference.  The external API key is a billing credential and can
    # be shared by unrelated harness tasks.  Explicit previous_response_id
    # links remain usable because they are resolved by the index itself.
    """从当前采集记录构建 ConversationIndex 并生成轨迹统计。

    输入：按采集顺序读取的 records；输出：(轨迹列表, 统计字典)。跳过未完成且
    不可从 SSE 恢复的响应，不写文件。unassigned 关闭前缀猜测；有明确
    previous_response_id 时仍尝试链接。统计中的 samples 是 turn 数，不是 SFT
    准入后的样本数量，也不证明任务级完整性。"""
    index = ConversationIndex(allow_unscoped_prefix=False); total = len(rows); skipped = 0; recovered = 0
    adapters = {Protocol.RESPONSES: ResponsesAdapter(), Protocol.CHAT: ChatAdapter()}
    completions = []
    record_issues = []
    for row in rows:
        problem = _record_shape_issue(row)
        if problem:
            record_issues.append({"request_id": row.get("request_id"), "reason": problem})
            skipped += 1
            continue
        try:
            response, recovered_row = _effective_response(row)
        except (ValueError, TypeError, IndexError, KeyError, AttributeError, OverflowError):
            # Provider JSON may be structurally valid but incompatible with a
            # delta accumulator. Isolate decoding errors, not programming/I/O
            # failures elsewhere in the exporter; retain the raw record.
            record_issues.append({"request_id": row.get("request_id"), "reason": "invalid_response_events"})
            skipped += 1
            continue
        if row["status"] != "completed" and not recovered_row:
            skipped += 1; continue
        if not isinstance(response, dict):
            skipped += 1; continue
        problem = _record_shape_issue({**row, "response": response})
        if problem:
            record_issues.append({"request_id": row.get("request_id"), "reason": problem})
            skipped += 1
            continue
        protocol = Protocol(row["protocol"]); adapter = adapters[protocol]
        target = adapter.extract_target(response)
        if not target: skipped += 1; continue
        request = row["request"]
        completion = CompletedCompletion(row["request_id"], row["run_id"], protocol, row["model"], request, response, row.get("response_id") or adapter.response_id(response), row.get("previous_response_id") or adapter.previous_response_id(request), adapter.extract_context(request), target, context_fingerprint(protocol=protocol, model=row["model"], request=request), session_key=extract_native_session_id(row.get("request_headers", {})), capture_status=row.get("status"), recovered_from_events=recovered_row)
        completions.append(completion)
        recovered += int(recovered_row)
    # Resolve explicit edges independently of capture arrival order. Within a
    # full-history set, shorter contexts precede longer contexts.
    ids: dict[tuple, list] = {}
    for completion in completions:
        if completion.response_id:
            ids.setdefault((completion.run_id, completion.session_key, completion.response_id), []).append(completion)
    pending = sorted(completions, key=lambda item: (len(item.context), item.request_id))
    added: set[str] = set()
    expanded = []
    while pending:
        ready = []
        for item in pending:
            parents = ids.get((item.run_id, item.session_key, item.previous_response_id), []) if item.previous_response_id else []
            if item.response_id and len(ids[(item.run_id, item.session_key, item.response_id)]) > 1:
                item.eligibility_reasons.append("duplicate_response_id")
            if len(parents) != 1 or parents[0].request_id in added:
                ready.append(item)
        if not ready:
            ready = list(pending)
            for item in ready:
                item.eligibility_reasons.append("cyclic_response_lineage")
        for item in ready:
            expanded.append(index.add(item).turns[-1])
            added.add(item.request_id)
            pending.remove(item)
    # All explicit-parent contexts are now known. Select one ready node at a
    # time so newly ready, shorter explicit continuations precede full prompts.
    index = ConversationIndex(allow_unscoped_prefix=False, expand_previous=False)
    pending = sorted(expanded, key=lambda item: (len(item.context), item.request_id))
    added = set()
    while pending:
        ready = [item for item in pending if not item.previous_response_id
                 or len(ids.get((item.run_id, item.session_key, item.previous_response_id), [])) != 1
                 or ids[(item.run_id, item.session_key, item.previous_response_id)][0].request_id in added]
        item = (ready or pending)[0]
        index.add(item)
        added.add(item.request_id)
        pending.remove(item)
    trajectories = index.export()
    stats = {"records_total": total, "trajectories": len(trajectories), "samples": sum(len(t.turns) for t in trajectories), "skipped": skipped}
    if record_issues:
        stats["record_issues"] = record_issues
    if recovered:
        stats["recovered_completed_streams"] = recovered
    tool_calls = tool_outputs = pending_tools = tool_issues = parallel_turns = 0
    for trajectory in trajectories:
        validation = validate_trajectory_tools(trajectory)
        items = trajectory.turns[-1].context + trajectory.turns[-1].target if trajectory.turns else []
        tool_calls += sum(item.get("type") in {"function_call", "custom_tool_call"} for item in items)
        tool_outputs += sum(item.get("type") in {"function_call_output", "custom_tool_call_output"} for item in items)
        pending_tools += len(validation.pending_call_ids)
        tool_issues += len(validation.issues)
        parallel_turns += int(validation.has_parallel_calls)
    if tool_calls or tool_outputs or tool_issues or pending_tools:
        stats.update({
            "tool_calls": tool_calls,
            "tool_outputs": tool_outputs,
            "pending_tool_calls": pending_tools,
            "tool_issues": tool_issues,
            "parallel_tool_trajectories": parallel_turns,
        })
    return trajectories, stats


def _event_terminal(protocol: Protocol, current: str, data: dict[str, Any] | None, *, done: bool = False) -> str:
    """按同一规则合并实时和回放帧终态；输入协议/状态/事件，返回保守状态。"""
    if data is None:
        return current if done else "failed"
    kind = data.get("type")
    if kind in {"error", "response.error"}:
        return "failed"
    if protocol == Protocol.RESPONSES and kind in {"response.completed", "response.failed", "response.incomplete"}:
        return _merge_terminal(current, kind.split(".")[1])
    if protocol == Protocol.CHAT:
        for choice in data.get("choices") or []:
            if not isinstance(choice, dict):
                return "failed"
            status = _chat_finish_status(choice.get("finish_reason"))
            if status:
                current = _merge_terminal(current, status)
    return current


def _effective_response(row: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
    """读取捕获响应，必要时重放 Responses SSE；输入记录，输出 (response, recovered)。
    失败/不完整或矛盾终态不恢复为成功；只有明确 completed 事件和完成快照可恢复。
    无 I/O，不把 HTTP 200 或 DONE 当作模型完成证据。"""
    stored = row.get("response")
    protocol = Protocol(row.get("protocol", "responses"))
    terminal = "partial"
    state = ResponsesStreamState() if protocol == Protocol.RESPONSES else None
    chat_chunks = []
    for event in row.get("events") or []:
        if not isinstance(event, dict):
            return None, False
        data = event.get("data")
        done = False
        if data is None:
            raw = event.get("raw")
            if not isinstance(raw, bytes):
                return None, False
            parser = SSEParser()
            frames = parser.feed(raw) + parser.finish()
            done = bool(frames) and all(frame.data.strip() == "[DONE]" for frame in frames)
        elif not isinstance(data, dict):
            return None, False
        elif not data.get("type") and isinstance(event.get("event"), str):
            data = {**data, "type": event["event"]}
        terminal = _event_terminal(protocol, terminal, data, done=done)
        if state is not None:
            state.consume(data)
        elif isinstance(data, dict):
            chat_chunks.append(data)
    if terminal in {"failed", "incomplete"}:
        return None, False
    if isinstance(stored, dict):
        if stored.get("status") in {"failed", "incomplete", "in_progress", "queued", "cancelled"}:
            return None, False
        if protocol == Protocol.CHAT:
            if _chat_finish_status(_chat_finish_reason(stored)) != "completed":
                return None, False
            # Reconstruct from original events so legacy snapshots cannot hide
            # unsupported delta fields removed by the earlier implementation.
            if chat_chunks:
                return _chat_snapshot(chat_chunks), False
        return stored, False
    if state is None or terminal != "completed":
        return None, False
    snapshot = state.finalize()
    return (snapshot.response, True) if isinstance(snapshot.response, dict) and snapshot.response.get("status") == "completed" else (None, False)


def _merge_terminal(current: str, incoming: str) -> str:
    """合并流终态，冲突固定为 failed；输入状态字符串，输出保守状态。"""
    if current == "partial":
        return incoming
    return current if incoming == current else "failed"


def _chat_finish_status(finish_reason: Any) -> str | None:
    """输入 Chat finish_reason，返回 completed、incomplete 或 None。
    stop/tool_calls/function_call 为自然结束；length/content_filter 不合格，DONE 不覆盖此判断。"""
    if not isinstance(finish_reason, str):
        return None
    if finish_reason in {"length", "content_filter"}:
        return "incomplete"
    if finish_reason in {"stop", "tool_calls", "function_call"}:
        return "completed"
    return None


def _chat_finish_reason(payload: Mapping[str, Any]) -> Any:
    """提取 Chat 第一个 choice 的终止原因。

    输入：响应 Mapping；输出：reason 原值或 None。无 I/O；仅处理有效列表的 choices[0]；畸形容器返回 None，
    不支持按多个候选分别判断完整性。"""
    choices = payload.get("choices") or []
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0].get("finish_reason")
    return None


def _chat_snapshot(chunks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """将 Chat SSE 增量拼成单一响应对象。

    输入：按到达顺序的 chunk 字典；输出：Chat response dict，空输入返回 None。
    仅内存计算；累积第一个 choice 的 content/tool_calls，保留 finish_reason。
    reasoning、refusal 和其他 choice 不进入首候选快照正文，但记录不支持字段标记，
    阻止有损快照进入正常 SFT；原始 SSE 另行存储。"""
    if not chunks:
        return None
    content: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    first = chunks[0]
    finish_reason = None
    unsupported = set()
    for chunk in chunks:
        choices = chunk.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            continue
        if len(choices) > 1 or any(choice.get("index", 0) != 0 for choice in choices):
            unsupported.add("multiple_completion_choices")
        choice = choices[0]
        finish_reason = choice.get("finish_reason") or finish_reason
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            raise ValueError("Chat delta must be an object")
        unsupported.update(key for key in delta if key not in {"role", "content", "tool_calls"} and delta[key] is not None)
        if isinstance(delta.get("content"), str):
            content.append(delta["content"])
        for call in delta.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            index = call.get("index", 0)
            if type(index) is not int or index < 0:
                raise ValueError("Chat tool index must be a nonnegative integer")
            current = tool_calls.setdefault(index, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
            current["id"] = current["id"] or call.get("id")
            function = call.get("function") or {}
            if isinstance(function.get("name"), str): current["function"]["name"] += function["name"]
            if isinstance(function.get("arguments"), str): current["function"]["arguments"] += function["arguments"]
    message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
    if unsupported:
        message["_sft_unsupported_fields"] = sorted(unsupported)
    if tool_calls: message["tool_calls"] = [tool_calls[k] for k in sorted(tool_calls)]
    return {"id": first.get("id"), "object": "chat.completion", "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}]}


def _response_headers(headers: httpx.Headers) -> dict[str, str]:
    """过滤上游响应头以匹配已解压的转发正文。

    输入：HTTPX Headers；输出：普通字典。移除 hop-by-hop、Connection 指定字段、
    Content-Length 和 Content-Encoding，保留缓存诊断等端到端字段。无 I/O；
    转换字典不保留同名 header 的独立出现次数。"""
    hop_by_hop = {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
        # httpx transparently decodes compressed response bodies.  Forwarding
        # this header would make the downstream client decode them a second
        # time and commonly produces a gzip/deflate decoding error.
        "content-encoding",
    }
    nominated = {part.strip().lower() for part in headers.get("connection", "").split(",") if part.strip()}
    return {key: value for key, value in headers.items() if key.lower() not in hop_by_hop and key.lower() not in nominated}


def _upstream_path(base_url: str, incoming_path: str) -> str:
    """输入 base URL 和路径，返回白名单相对端点；非法路径抛 HTTP 400。

    在全部前缀处理之后校验，固定 scheme/host/port；不接受用户构造的 URL。
    """
    path = incoming_path.lstrip("/")
    leaf = urlsplit(base_url).path.rstrip("/").rsplit("/", 1)[-1]
    if leaf and path.startswith(leaf + "/"):
        path = path[len(leaf) + 1:]
    if path not in {"responses", "chat/completions", "models", "v1/responses", "v1/chat/completions", "v1/models"}:
        raise HTTPException(status_code=400, detail="unsupported upstream endpoint")
    base = httpx.URL(base_url.rstrip("/") + "/")
    target = base.join(path)
    if (target.scheme, target.host, target.port) != (base.scheme, base.host, base.port):
        raise HTTPException(status_code=400, detail="upstream origin mismatch")
    return path


_HOP_BY_HOP_REQUEST_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
}
_PRIVATE_REQUEST_HEADERS = {
    "authorization", "proxy-authorization", "x-api-key", "x-goog-api-key",
    "x-admin-key", "x-gateway-key", "cookie", "set-cookie",
}


def _forward_request_headers(headers: httpx.Headers) -> dict[str, str]:
    """复制可转发的请求头。

    输入：HTTPX/兼容 Headers；输出：去掉认证、cookie、hop-by-hop 和 Connection
    指定字段的字典。无 I/O；上游 Authorization 由调用者单独重建，其他未列出
    的自定义 header 原样保留，不是任意秘密自动检测器。"""
    blocked = _HOP_BY_HOP_REQUEST_HEADERS | _PRIVATE_REQUEST_HEADERS
    nominated = {part.strip().lower() for part in headers.get("connection", "").split(",") if part.strip()}
    blocked |= nominated
    return {k: v for k, v in headers.items() if k.lower() not in blocked}


def _json_record(record: dict[str, Any]) -> dict[str, Any]:
    """将内部采集记录转为管理 API 可序列化形式。

    输入：含可选 bytes raw_body/events 的字典；输出：浅拷贝及复制的事件列表，
    其中 bytes 编为 base64。无 I/O、不修改输入；不会删除正文中业务数据或秘密，
    所以该输出必须由管理接口权限保护。"""
    value = dict(record)
    for key in ("raw_body", "request_body", "forwarded_body"):
        if isinstance(value.get(key), bytes):
            value[key] = base64.b64encode(value[key]).decode("ascii")
    events = []
    for event in value.get("events", []):
        item = dict(event)
        if isinstance(item.get("raw"), bytes):
            item["raw"] = base64.b64encode(item["raw"]).decode("ascii")
        events.append(item)
    value["events"] = events
    return value


# Lazy ASGI wrapper keeps CLI imports free of database side effects.
async def app(scope: Any, receive: Any, send: Any) -> None:
    """延迟创建默认 ASGI 应用；输入 ASGI 三元组，输出协议事件，无导入时 DB 写入。"""
    global _default_app
    if _default_app is None:
        _default_app = make_app()
    await _default_app(scope, receive, send)


_default_app: FastAPI | None = None
