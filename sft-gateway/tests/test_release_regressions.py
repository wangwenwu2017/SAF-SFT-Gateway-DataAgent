"""交付回归：固定上游安全、端到端终态、因果图、文本资格和导出生命周期。

所有 HTTP 请求都使用内存 MockTransport；数据库和文件只写 pytest 临时目录。
"""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from sft_gateway.app import make_app, _build_export, _effective_response
from sft_gateway.cli import main
from sft_gateway.export.codex import export_codex_samples
from sft_gateway.export.eligibility import rejection_reasons
from sft_gateway.gateway.storage import Storage
from test_conversation_index import build, completion, message, output


def test_fixed_billing_key_requires_gateway_key(monkeypatch, tmp_path):
    monkeypatch.setenv('SFT_UPSTREAM_API_KEY', 'fake-billing')
    with pytest.raises(ValueError, match='SFT_GATEWAY_API_KEY'):
        make_app('https://safe.test/v1', str(tmp_path / 'db'))


@pytest.mark.parametrize('path', ['/v1/http://evil.test/v1/responses', '/http://evil.test/responses', '/v1/https:%2f%2fevil.test/responses', '/v1/../http://evil.test/responses', '/v1/%5cevil.test/responses', '/v1/responses/extra'])
def test_no_credential_leaves_fixed_origin(monkeypatch, tmp_path, path):
    monkeypatch.setenv('SFT_UPSTREAM_API_KEY', 'fake-billing')
    monkeypatch.setenv('SFT_GATEWAY_API_KEY', 'fake-gateway')
    seen = []
    async def run():
        app = make_app('https://safe.test/v1', str(tmp_path / 'db'), transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, json={})))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway') as client:
                response = await client.post(path, headers={'Authorization': 'Bearer fake-gateway'}, json={'input': 'x'})
            assert response.status_code == 400
            assert not seen
        finally:
            app.state.storage.close()
    asyncio.run(run())


def test_management_disabled_without_admin(monkeypatch, tmp_path):
    monkeypatch.delenv('SFT_ADMIN_API_KEY')
    async def run():
        app = make_app('https://safe.test', str(tmp_path / 'db'))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway') as client:
                assert (await client.get('/runs/private/completions')).status_code == 403
                assert (await client.post('/v1/responses', json={'input': 'x'})).status_code == 401
        finally:
            app.state.storage.close()
    asyncio.run(run())


@pytest.mark.parametrize('reason,expected', [('stop', 1), ('length', 0), ('content_filter', 0), (None, 0)])
def test_chat_done_does_not_upgrade_finish_reason(tmp_path, reason, expected):
    chunks = [{'id': 'r', 'choices': [{'index': 0, 'delta': {'content': 'answer'}, 'finish_reason': reason}]}]
    wire = b''.join(('data: ' + json.dumps(chunk) + '\n\n').encode() for chunk in chunks) + b'data: [DONE]\n\n'
    async def run():
        app = make_app('https://safe.test', str(tmp_path / 'db'), transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=wire)))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway') as client:
                result = await client.post('/runs/a/v1/chat/completions', headers={'Authorization': 'Bearer fake'}, json={'model': 'm', 'messages': [{'role': 'user', 'content': 'x'}], 'stream': True})
                assert result.content == wire
            trajectories, _ = _build_export(app.state.storage.records('a'))
            assert len(list(export_codex_samples(trajectories))) == expected
        finally:
            app.state.storage.close()
    asyncio.run(run())


def test_contradictory_responses_not_recovered():
    events = [{'data': {'type': 'response.' + state, 'response': {'id': 'r', 'status': state, 'output': [output('a')]}}} for state in ['completed', 'failed']]
    assert _effective_response({'protocol': 'responses', 'response': None, 'events': events}) == (None, False)


@pytest.mark.parametrize('item', [{'type': 'compaction', 'encrypted_content': 'opaque'}, {'type': 'item_reference', 'id': 'hidden'}, {'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'x'}]}])
def test_unsupported_context_rejected(item):
    trajectories = build(completion('1', [message('user', 'x'), item], [output('a')]))
    assert rejection_reasons(trajectories[0])
    assert list(export_codex_samples(trajectories)) == []


def test_opaque_only_target_rejected():
    trajectories = build(completion('1', [message('user', 'x')], [{'type': 'reasoning', 'encrypted_content': 'x'}]))
    assert 'empty_visible_target' in rejection_reasons(trajectories[0])


def test_tools_change_preserves_lineage_but_rejects_training():
    first = completion('1', [message('user', 'x')], [output('a')], tools=[{'type': 'function', 'name': 'one'}])
    second = completion('2', first.context + first.target + [message('user', 'y')], [output('b')], tools=[{'type': 'function', 'name': 'two'}])
    trajectories = build(first, second)
    assert len(trajectories) == 1
    assert [turn.request_id for turn in trajectories[0].turns] == ['1', '2']
    assert list(export_codex_samples(trajectories)) == []


def test_json_arguments_normalize_but_custom_text_does_not():
    call = {'type': 'function_call', 'call_id': 'a', 'name': 'exec', 'arguments': '{"a":1,"b":2}'}
    first = completion('1', [message('user', 'x')], [call])
    resent = {**call, 'arguments': '{ "b": 2, "a": 1 }'}
    result = {'type': 'function_call_output', 'call_id': 'a', 'output': 'ok'}
    trajectories = build(first, completion('2', first.context + [resent, result], [output('done')]))
    assert len(list(export_codex_samples(trajectories))) == 1
    custom = {'type': 'custom_tool_call', 'call_id': 'a', 'name': 'exec', 'input': '{ "a": 1 }'}
    first = completion('1', [message('user', 'x')], [custom])
    trajectories = build(first, completion('2', first.context + [{**custom, 'input': '{"a":1}'}, {'type': 'custom_tool_call_output', 'call_id': 'a', 'output': 'ok'}], [output('done')]))
    assert list(export_codex_samples(trajectories)) == []


def test_graph_resolves_out_of_order_and_rejects_cycles():
    def row(item):
        return {'request_id': item.request_id, 'run_id': item.run_id, 'model': item.model, 'protocol': 'responses', 'status': 'completed', 'request': item.request, 'response': item.response, 'response_id': item.response_id, 'previous_response_id': item.previous_response_id}
    first = completion('1', [message('user', 'x')], [output('a')])
    second = completion('2', [message('user', 'y')], [output('b')], previous='1')
    forward, _ = _build_export([row(first), row(second)])
    backward, _ = _build_export([row(second), row(first)])
    assert list(export_codex_samples(forward)) == list(export_codex_samples(backward))
    first.previous_response_id = '2'
    cyclic, _ = _build_export([row(first), row(second)])
    assert not list(export_codex_samples(cyclic))


def test_sse_commit_releases_lock_and_concurrent_sequences(tmp_path):
    path = tmp_path / 'db'
    stores = [Storage(path), Storage(path)]
    try:
        stores[0].start_request('first', 'run', 'responses', '/v1/responses', 'm', {}, {})
        stores[0].add_event('first', 0, None, {}, b'data: {}\n\n')
        assert not stores[0].db.in_transaction
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(stores[index % 2].start_request, str(index), 'run', 'responses', '/v1/responses', 'm', {}, {}) for index in range(20)]
            assert sorted(f.result(timeout=5) for f in futures) == list(range(2, 22))
    finally:
        for store in stores:
            store.close()


def test_cli_help_is_readonly_and_child_exit_propagates(tmp_path, monkeypatch):
    db = tmp_path / 'never-created'
    monkeypatch.setenv('SFT_DB', str(db))
    assert subprocess.run([sys.executable, '-m', 'sft_gateway.cli', '--help'], capture_output=True).returncode == 0
    assert not db.exists()
    monkeypatch.setenv('OPENAI_BASE_URL', 'http://wrong')
    code = "import os,sys; sys.exit(7 if os.environ['OPENAI_BASE_URL']=='http://gateway/runs/a/v1' else 9)"
    assert main(['run', '--gateway', 'http://gateway', '--run-id', 'a', '--', sys.executable, '-c', code]) == 7


def test_export_manifest_and_stale_cleanup(tmp_path):
    db = tmp_path / 'db'
    store = Storage(db)
    store.start_request('1', 'a', 'responses', '/v1/responses', 'm', {'input': 'x'}, {})
    store.finish('1', status='completed', status_code=200, response={'id': '1', 'status': 'completed', 'output': [output('a')]}, raw_body=b'\xff', response_id='1', previous_response_id=None)
    store.close()
    dest = tmp_path / 'export'
    assert main(['export', 'a', '--db', str(db), '--output', str(dest)]) == 0
    manifest = json.loads((dest / 'manifest.json').read_text())
    for name, digest in manifest['files'].items():
        assert hashlib.sha256((dest / name).read_bytes()).hexdigest() == digest
    assert json.loads((dest / 'raw_records.jsonl').read_text())['raw_body'] == '/w=='
    (dest / 'sample.incomplete.json').write_text('stale')
    assert main(['export', 'empty', '--db', str(db), '--output', str(dest)]) == 0
    assert not (dest / 'sample.json').exists()
    assert not (dest / 'sample.incomplete.json').exists()
    assert json.loads((dest / 'sft_samples.json').read_text()) == []


def test_slow_sse_does_not_block_second_worker(tmp_path):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"type":"response.created","response":{"id":"slow"}}\n\n'
                started.set()
                await release.wait()
                yield ('data: ' + json.dumps({'type': 'response.completed', 'response': {'id': 'slow', 'status': 'completed', 'output': [output('a')]}}) + '\n\n').encode()
        def upstream(request):
            body = json.loads(request.content)
            if body.get('stream'):
                return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=SlowStream())
            return httpx.Response(200, json={'id': 'fast', 'status': 'completed', 'output': [output('b')]})
        apps = [make_app('http://upstream', str(tmp_path / 'db'), transport=httpx.MockTransport(upstream)) for _ in range(2)]
        clients = [httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway', headers={'Authorization': 'Bearer fake'}) for app in apps]
        slow = asyncio.create_task(clients[0].post('/runs/slow/v1/responses', json={'input': 'x', 'stream': True}))
        try:
            await asyncio.wait_for(started.wait(), 3)
            fast = await asyncio.wait_for(clients[1].post('/runs/fast/v1/responses', json={'input': 'y'}), 3)
            assert fast.status_code == 200
            assert not slow.done()
            assert len(apps[1].state.storage.records('slow')[0]['events']) == 1
        finally:
            release.set()
            await asyncio.wait_for(slow, 3)
            for client, app in zip(clients, apps):
                await client.aclose()
                app.state.storage.close()
    asyncio.run(run())


def test_capture_preserves_wire_bytes_and_enforces_limits(tmp_path, monkeypatch):
    monkeypatch.setenv('SFT_MAX_REQUEST_BYTES', '128')
    monkeypatch.setenv('SFT_MAX_RESPONSE_BYTES', '256')
    wire = b'{ "model": "m", "input": "x" }'
    seen = []
    def upstream(request):
        seen.append(request.content)
        if b'large' in request.content:
            return httpx.Response(200, content=b'x' * 257)
        return httpx.Response(200, json={'id': 'r', 'status': 'completed', 'output': [output('ok')]})
    async def run():
        app = make_app('http://upstream', str(tmp_path / 'db'), transport=httpx.MockTransport(upstream))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway', headers={'Authorization': 'Bearer fake'}) as client:
                assert (await client.post('/runs/a/v1/responses', content=wire)).status_code == 200
                assert seen == [wire]
                row = app.state.storage.records('a')[0]
                assert row['request_body'] == row['forwarded_body'] == wire
                assert (await client.post('/runs/a/v1/responses', content=b'x' * 129)).status_code == 413
                assert (await client.post('/runs/a/v1/responses', json={'input': 'large'})).status_code == 502
                assert app.state.storage.records('a')[-1]['status'] == 'upstream_error'
        finally:
            app.state.storage.close()
    asyncio.run(run())


def test_unobserved_repeated_assistant_input_is_not_supervision():
    user, answer = message('user', 'x'), output('a')
    trajectories = build(completion('1', [user], [answer]), completion('2', [user, answer], [output('b')], previous='1'))
    leaf = trajectories[0].turns[-1]
    assert leaf.context == [user, answer, user, answer]
    assert 'unobserved_assistant_history' in rejection_reasons(trajectories[0])
    assert list(export_codex_samples(trajectories)) == []
