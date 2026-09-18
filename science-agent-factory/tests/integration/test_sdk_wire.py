"""本机真实 Codex CLI 到假 Responses 服务的参数验证。

测试目的：检查固定版本 SDK/CLI 的模型、推理强度实际线上请求字段，不调用外部模型。
被测接口：CodexConfig、AsyncCodex、SDK 取消适配与原生 turn。
输入与夹具：临时 CODEX_HOME、回环 HTTP 服务、占位认证和最小 SSE 响应。
预期结果：请求包含原样模型名及 low effort，正常接收完成事件并有界退出。
"""

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox

from science_agent_factory.agents.sdk_control import (
    close_sdk,
    install_queue_wakeup,
    run_observed_turn,
)


async def test_real_sdk_sends_model_and_reasoning_to_responses(tmp_path):
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append({k: request.get(k) for k in ("model", "reasoning")})
            item = {
                "type": "message",
                "id": "msg-local",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "ok", "annotations": []}],
            }
            response = {
                "id": "resp-local",
                "object": "response",
                "status": "completed",
                "output": [item],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }
            events = [
                {
                    "type": "response.created",
                    "response": {**response, "status": "in_progress", "output": []},
                },
                {"type": "response.output_item.added", "output_index": 0, "item": item},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ]
            body = "".join(
                "event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    env = {
        k: v
        for k, v in os.environ.items()
        if "proxy" not in k.lower()
        and k not in {"OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY", "SAF_CODEX_API_KEY"}
    }
    (tmp_path / "home").mkdir()
    env.update(CODEX_HOME=str(tmp_path / "home"), WIRE_TEST_KEY="local-placeholder")
    config = CodexConfig(
        cwd=str(tmp_path),
        env=env,
        config_overrides=(
            'model_provider="saf_api"',
            'model_reasoning_effort="low"',
            'model_providers.saf_api.name="saf_api"',
            f'model_providers.saf_api.base_url="http://127.0.0.1:{server.server_port}/v1"',
            'model_providers.saf_api.env_key="WIRE_TEST_KEY"',
            'model_providers.saf_api.wire_api="responses"',
        ),
    )
    sdk = AsyncCodex(config)
    install_queue_wakeup(sdk)
    try:
        async with asyncio.timeout(30):
            await sdk.__aenter__()
            thread = await sdk.thread_start(
                model="bailian/deepseek-v4.1-flash",
                model_provider="saf_api",
                approval_mode=ApprovalMode.deny_all,
                sandbox=Sandbox.full_access,
                ephemeral=True,
                cwd=str(tmp_path),
                base_instructions="Reply ok, no tools.",
            )
            result = await run_observed_turn(
                thread, "Reply ok.", timeout_s=15, exploration_s=0, emit=lambda event: None
            )
            assert result.final_response == "ok"
        assert captured
        assert captured[-1]["model"] == "bailian/deepseek-v4.1-flash"
        assert captured[-1]["reasoning"]["effort"] == "low", captured
    finally:
        await close_sdk(sdk)
        server.shutdown()
        server.server_close()
        worker.join(2)
