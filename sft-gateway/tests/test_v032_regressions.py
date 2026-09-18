"""Regression inputs for malformed content and asynchronous, interleaved histories."""
import json
import asyncio
import httpx
from itertools import permutations

import pytest

from test_conversation_index import completion, message, output
from test_v031_regressions import row
from sft_gateway.app import _build_export
from sft_gateway.export.codex import export_codex_samples
from sft_gateway.export.eligibility import _text_supported
from sft_gateway.protocols.responses_state import ResponsesStreamState


@pytest.mark.parametrize('kind', [{}, [], 3, True])
@pytest.mark.parametrize('field', ['content', 'summary'])
def test_malformed_content_record_does_not_abort_other_task(kind, field):
    bad_item = ({'type': 'reasoning'} if field == 'summary' else
                {'type': 'message', 'role': 'assistant'})
    bad_item[field] = [{'type': kind, 'text': 'bad'}]
    bad = row(completion('bad', [message('user', 'q')], [bad_item], run_id='bad'))
    good = row(completion('good', [message('user', 'q')], [output('ok')], run_id='good'))
    trajectories, stats = _build_export([bad, good])
    samples = list(export_codex_samples(trajectories))
    assert len(samples) == 1
    assert samples[0]['messages'][-1]['content'] == 'ok'
    assert stats['record_issues'] == [{'request_id': 'bad', 'reason': 'invalid_content_part_type'}]


@pytest.mark.parametrize('kind', [{}, []])
def test_text_eligibility_is_total_for_json_content(kind):
    assert not _text_supported([{'type': kind, 'text': 'bad'}])


def test_delayed_tool_results_preserve_causality_for_every_arrival_order():
    user = message('user', 'work')
    calls = [{'type': 'function_call', 'call_id': cid, 'name': 'shell', 'arguments': '{}'}
             for cid in ('a', 'b', 'c')]
    results = [{'type': 'function_call_output', 'call_id': cid, 'output': cid}
               for cid in ('b', 'c', 'a')]
    middle = output('Only b has returned; still waiting for a and c.')
    rows = [row(completion('1', [user], calls, run_id='task')),
            row(completion('2', [results[0]], [middle], previous='1', run_id='task')),
            row(completion('3', [user, *calls, results[0], middle, *results[1:]],
                           [output('done')], run_id='task'))]
    for ordered in permutations(rows):
        trajectories, _ = _build_export(list(ordered))
        sample, = export_codex_samples(trajectories)
        messages = sample['messages']
        assert [m['role'] for m in messages] == [
            'user', 'tool_call', 'tool_call', 'tool_call', 'tool_response',
            'assistant', 'tool_response', 'tool_response', 'assistant']
        assert messages[5]['content'] == middle['content'][0]['text']
        assert [json.loads(messages[i]['content'])['call_id'] for i in (4, 6, 7)] == ['b', 'c', 'a']


def test_interleaved_tasks_with_identical_prompts_never_merge():
    user = message('user', 'same')
    rows = [row(completion(scope + '1', [user], [output('a')], run_id=scope)) for scope in ('A', 'B')]
    rows += [row(completion(scope + '2', [user, output('a'), message('user', 'next')],
                            [output(scope)], run_id=scope)) for scope in ('A', 'B')]
    for ordered in permutations(rows):
        trajectories, _ = _build_export(list(ordered))
        assert {tuple(t.request_id for t in tr.turns) for tr in trajectories} == {('A1', 'A2'), ('B1', 'B2')}


@pytest.mark.parametrize('events', [
    [{'type': 'response.output_text.delta', 'content_index': -1, 'delta': 'a'}],
    [{'type': 'response.output_item.added', 'item': {'type': 'message', 'content': None}, 'output_index': 0},
     {'type': 'response.output_text.delta', 'output_index': 0, 'delta': 'a'}],
    [{'type': 'response.content_part.added', 'output_index': 0, 'part': {'type': 'output_text', 'text': {}}},
     {'type': 'response.output_text.delta', 'output_index': 0, 'delta': 'a'}],
])
def test_malformed_response_events_are_isolated(events):
    good = row(completion('good', [message('user', 'q')], [output('ok')], run_id='good'))
    bad = {**good, 'request_id': 'bad', 'status': 'partial', 'response': None,
           'events': [{'data': e} for e in events]}
    trajectories, stats = _build_export([bad, good])
    assert len(list(export_codex_samples(trajectories))) == 1
    assert stats['record_issues'][0]['request_id'] == 'bad'


@pytest.mark.parametrize('delta', ['hi', {'tool_calls': [{'index': 'x', 'function': {'name': 'f', 'arguments': '{}'}}]}])
def test_malformed_chat_events_are_isolated(delta):
    good = row(completion('good', [message('user', 'q')], [output('ok')], run_id='good'))
    bad = {**good, 'request_id': 'bad', 'protocol': 'chat',
           'request': {'messages': [message('user', 'q')]},
           'response': {'choices': [{'message': message('assistant', 'hi'), 'finish_reason': 'stop'}]},
           'events': [{'data': {'choices': [{'delta': delta, 'finish_reason': 'stop'}]}}]}
    trajectories, stats = _build_export([bad, good])
    assert len(list(export_codex_samples(trajectories))) == 1
    assert stats['record_issues'][0]['request_id'] == 'bad'


def test_chat_multiple_stream_choices_cannot_become_one_training_target():
    from sft_gateway.app import _chat_snapshot
    chunks = [{'choices': [{'index': i, 'delta': {'content': text}, 'finish_reason': 'stop'}
                           for i, text in enumerate(('left', 'right'))]}]
    record = {'request_id': 'chat', 'run_id': 'task', 'protocol': 'chat', 'model': 'm', 'status': 'completed',
              'request': {'messages': [message('user', 'q')]}, 'response': _chat_snapshot(chunks),
              'events': [{'data': c} for c in chunks]}
    trajectories, _ = _build_export([record])
    assert not list(export_codex_samples(trajectories))


def test_sparse_content_index_is_rejected_without_allocating_placeholders():
    state = ResponsesStreamState()
    with pytest.raises(ValueError, match='content part index'):
        state.consume({'type': 'response.output_text.delta', 'content_index': 2, 'delta': 'a'})


@pytest.mark.parametrize('protocol,payload', [
    ('responses', {'status': [], 'output': []}),
    ('chat/completions', {'choices': {'x': 1}}),
    ('chat/completions', {'choices': 1}),
    ('chat/completions', {'choices': [{'message': {'role': 'assistant', 'content': 'hi'}, 'finish_reason': {}}]}),
])
def test_malformed_nonstream_upstream_is_forwarded_and_captured(tmp_path, protocol, payload):
    from sft_gateway.app import make_app
    raw = json.dumps(payload).encode()
    async def run():
        app = make_app('http://upstream', str(tmp_path/'capture.sqlite'),
                       transport=httpx.MockTransport(lambda request: httpx.Response(200, content=raw)))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway',
                                         headers={'Authorization': 'Bearer fake'}) as client:
                response = await client.post('/runs/check/v1/' + protocol, json={'input': 'hi', 'messages': []})
                assert response.status_code == 200
                assert response.content == raw
                record, = app.state.storage.records('check')
                assert record['raw_body'] == raw
                assert record['status'] != 'in_flight'
                trajectories, _ = _build_export([record])
                assert not list(export_codex_samples(trajectories))
        finally:
            app.state.storage.close()
    asyncio.run(run())


def test_malformed_sse_does_not_interrupt_raw_forwarding(tmp_path):
    from sft_gateway.app import make_app
    events = [{'type': 'response.output_text.delta', 'content_index': -1, 'delta': 'bad'},
              {'type': 'response.completed', 'response': {'id': 'r', 'status': 'completed', 'output': [output('ok')]}}]
    raw = b''.join(('data: ' + json.dumps(e) + '\n\n').encode() for e in events)
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in raw.splitlines(keepends=True):
                yield chunk
    async def run():
        app = make_app('http://upstream', str(tmp_path/'capture.sqlite'), transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Stream())))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway',
                                         headers={'Authorization': 'Bearer fake'}) as client:
                response = await client.post('/runs/check/v1/responses', json={'input': 'hi', 'stream': True})
                assert response.content == raw
                record, = app.state.storage.records('check')
                assert record['raw_body'] == raw
                assert len(record['events']) == 2
                assert record['status'] == 'failed'
                trajectories, _ = _build_export([record])
                assert not list(export_codex_samples(trajectories))
        finally:
            app.state.storage.close()
    asyncio.run(run())
