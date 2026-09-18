"""0.3.1：混合因果链接、流状态回放、字段保真及异常记录隔离。"""
from itertools import permutations
import pytest
from test_conversation_index import completion, message, output
from sft_gateway.app import _build_export, _chat_snapshot
from sft_gateway.export.codex import export_codex_samples


def row(turn):
    return dict(request_id=turn.request_id, run_id=turn.run_id, protocol=turn.protocol.value,
                model=turn.model, status='completed', request=turn.request, response=turn.response,
                response_id=turn.response_id, previous_response_id=turn.previous_response_id)


def test_mixed_links_all_arrival_orders():
    u,v,w=[message('user',x) for x in 'uvw'];a,b,c=[output(x) for x in 'abc']
    rows=[row(completion('1',[u],[a],run_id='task')),row(completion('2',[v],[b],previous='1',run_id='task')),
          row(completion('3',[u,a,v,b,w],[c],run_id='task'))]
    for ordered in permutations(rows):
        trajectories,_=_build_export(list(ordered))
        assert [[t.request_id for t in tr.turns] for tr in trajectories]==[['1','2','3']]
        assert list(export_codex_samples(trajectories))[0]['messages'][-1]['content']=='c'


def test_bad_record_isolated():
    good=row(completion('good',[message('user','q')],[output('a')],run_id='task'))
    bad={**good,'request_id':'bad','response':{'id':'bad','status':'completed','output':None}}
    trajectories,stats=_build_export([good,bad])
    assert len(list(export_codex_samples(trajectories)))==1
    assert stats['record_issues'][0]['request_id']=='bad'


@pytest.mark.parametrize('field',['reasoning_content','refusal','audio','function_call'])
def test_chat_stream_unsupported_fields_do_not_disappear(field):
    snapshot=_chat_snapshot([{'id':'r','choices':[{'index':0,'delta':{'content':'a',field:'opaque'},'finish_reason':'stop'}]}])
    tr,_=_build_export([dict(request_id='r',run_id='task',protocol='chat',model='m',status='completed',
       request={'model':'m','messages':[message('user','q')]},response=snapshot)])
    assert list(export_codex_samples(tr))==[]


@pytest.mark.parametrize('reason',['length','content_filter'])
def test_legacy_chat_terminal_rechecked(reason):
    response={'id':'r','choices':[{'message':message('assistant','partial'),'finish_reason':reason}]}
    tr,_=_build_export([dict(request_id='r',run_id='task',protocol='chat',model='m',status='completed',
       request={'model':'m','messages':[message('user','q')]},response=response)])
    assert list(export_codex_samples(tr))==[]


def test_invalid_frame_cannot_recover_completed():
    response={'id':'r','status':'completed','output':[output('a')]}
    events=[{'data':None,'raw':b'data: {invalid}\n\n'},
            {'data':{'type':'response.completed','response':response}}]
    tr,_=_build_export([dict(request_id='r',run_id='task',protocol='responses',model='m',status='partial',
       request={'model':'m','input':'q'},response=None,events=events)])
    assert list(export_codex_samples(tr))==[]


def test_legacy_chat_conflicting_events_rejected():
    chunks=[{'choices':[{'delta':{'content':'a'},'finish_reason':'length'}]},
            {'choices':[{'delta':{},'finish_reason':'stop'}]}]
    tr,_=_build_export([dict(request_id='r',run_id='task',protocol='chat',model='m',status='completed',
       request={'messages':[message('user','q')]},response=_chat_snapshot(chunks),events=[{'data':c} for c in chunks])])
    assert not list(export_codex_samples(tr))


@pytest.mark.parametrize('bad_message,reason',[(message('assistant','a'),{}),({'role':'assistant','content':'a','tool_calls':42},'stop')])
def test_nested_invalid_chat_isolated(bad_message,reason):
    good=row(completion('good',[message('user','q')],[output('a')],run_id='task'))
    bad=dict(request_id='bad',run_id='task',protocol='chat',model='m',status='completed',
             request={'messages':[message('user','q')]},response={'choices':[{'message':bad_message,'finish_reason':reason}]})
    tr,stats=_build_export([good,bad])
    assert len(list(export_codex_samples(tr)))==1
    assert stats['record_issues'][0]['request_id']=='bad'


def test_recovered_bad_response_isolated():
    good=row(completion('good',[message('user','q')],[output('a')],run_id='task'))
    bad={**good,'request_id':'bad','response':None,'status':'partial','events':[{'data':{'type':'response.completed','response':{'id':'bad','status':'completed','output':None}}}]}
    tr,stats=_build_export([good,bad])
    assert len(list(export_codex_samples(tr)))==1
    assert stats['record_issues'][0]['request_id']=='bad'
