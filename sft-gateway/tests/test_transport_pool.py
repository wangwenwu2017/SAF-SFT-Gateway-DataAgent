"""Pooled upstream lifecycle and durable chunk transactions, using an offline transport."""
from __future__ import annotations

import asyncio
import json
import time
import tracemalloc

import httpx

from sft_gateway.app import make_app


def test_long_stream_batches_events_and_reuses_client(tmp_path, monkeypatch):
    """Two requests share a pool; every forwarded event is committed and pool closes at shutdown."""
    monkeypatch.delenv('SFT_UPSTREAM_API_KEY', raising=False)
    monkeypatch.delenv('SFT_GATEWAY_API_KEY', raising=False)
    chunks = [b'data: ' + json.dumps({'choices': [{'delta': {'content': 'x' * 128}, 'finish_reason': None}]}).encode() + b'\n\n'
              for _ in range(2000)]
    chunks += [b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n', b'data: [DONE]\n\n']
    raw = b''.join(chunks)
    async def response(request):
        return httpx.Response(200, content=raw, headers={'content-type': 'text/event-stream'})
    app = make_app('https://fixture.invalid/v1', str(tmp_path / 'capture.sqlite'), transport=httpx.MockTransport(response))
    storage = app.state.storage
    batches = []
    original = storage.add_events
    def record(request_id, events):
        original(request_id, events)
        batches.append(len(events))
    monkeypatch.setattr(storage, 'add_events', record)
    async def exercise():
        async with app.router.lifespan_context(app):
            pool = app.state.upstream_client
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://gateway', headers={'authorization': 'Bearer fixture'}) as client:
                for _ in range(2):
                    result = await client.post('/v1/chat/completions', json={'model': 'fixture', 'stream': True,
                                                                          'messages': [{'role': 'user', 'content': 'test'}]})
                    assert result.content == raw
                    assert app.state.upstream_client is pool
            assert not pool.is_closed
        assert pool.is_closed
    tracemalloc.start()
    started = time.monotonic()
    try:
        asyncio.run(exercise())
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert sum(batches) == 4004
    assert max(batches) > 1
    assert len(batches) < 100
    assert peak < 128 * 1024 * 1024
    print(json.dumps({'stream_bytes': len(raw), 'peak_bytes': peak,
                      'elapsed_sec': time.monotonic() - started, 'transactions': len(batches)}))
