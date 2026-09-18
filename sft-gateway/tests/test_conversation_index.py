"""Behavior tests: assert exact SFT histories, not just trajectory counts.

前缀、父响应、分支与工具顺序回归。输入为构造完成记录；输出为精确历史和样本断言。部分现有期望沿用旧 instructions，不可替代 Responses 官方语义校验。
"""

from copy import deepcopy
import json

import pytest

from sft_gateway.export.messages import export_samples, export_trajectories
from sft_gateway.export.codex import export_codex_samples, has_unresolved_tool_calls
from sft_gateway.gateway.detection import Protocol
from sft_gateway.protocols.chat import ChatAdapter
from sft_gateway.protocols.responses import ResponsesAdapter
from sft_gateway.trajectory.context_fingerprint import context_fingerprint
from sft_gateway.trajectory.conversation_index import ConversationIndex
from sft_gateway.trajectory.models import CompletedCompletion
from sft_gateway.trajectory.prefix import PrefixMatch, find_longest_prompt_prefix


def message(role, text):
    return {"role": role, "content": text}


def test_prefix_match_is_longest_and_ties_are_explicit():
    prompt = [1, 2, 3, 4]
    assert find_longest_prompt_prefix(prompt, [[1], [1, 2, 3], [1, 2]]) == PrefixMatch(1, 3)
    assert find_longest_prompt_prefix(prompt, [[1, 2], [1, 2]]).reason == "ambiguous_prefix"
    assert find_longest_prompt_prefix([1], [[1]]).reason == "no_prefix_match"


def output(text):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def completion(rid, prompt, target, *, protocol=Protocol.RESPONSES, previous=None, run_id="unassigned", **settings):
    request = {"model": "mock", **settings}
    request["input" if protocol == Protocol.RESPONSES else "messages"] = prompt
    if previous:
        request["previous_response_id"] = previous
    adapter = ResponsesAdapter() if protocol == Protocol.RESPONSES else ChatAdapter()
    response = {"id": rid, "output": target} if protocol == Protocol.RESPONSES else {"id": rid, "choices": [{"message": target[0]}]}
    return CompletedCompletion(rid, run_id, protocol, request["model"], request, response, rid, previous, adapter.extract_context(request), target, context_fingerprint(protocol=protocol, model=request["model"], request=request))


def build(*turns):
    index = ConversationIndex()
    for turn in turns:
        index.add(turn)
    return index.export()


@pytest.mark.parametrize("protocol", list(Protocol))
def test_full_history_never_duplicates_prior_answer(protocol):
    a = output("a") if protocol == Protocol.RESPONSES else message("assistant", "a")
    b = output("b") if protocol == Protocol.RESPONSES else message("assistant", "b")
    u, v = message("user", "u"), message("user", "v")
    trajectories = build(completion("1", [u], [a], protocol=protocol), completion("2", [u, a, v], [b], protocol=protocol))
    assert len(trajectories) == 1
    assert list(export_trajectories(trajectories))[0]["messages"] == [u, message("assistant", "a"), v, message("assistant", "b")]


def test_incremental_sample_contains_previous_answer_and_instructions_once():
    u, v = message("user", "u"), message("user", "v")
    trajectories = build(completion("1", [u], [output("a")], instructions="sys"), completion("2", [v], [output("b")], previous="1", instructions="sys"))
    expected = [message("system", "sys"), u, message("assistant", "a"), v, message("assistant", "b")]
    assert list(export_samples(trajectories))[1]["messages"] == expected
    assert list(export_trajectories(trajectories))[0]["messages"] == expected


def test_continuation_drops_request_instructions_and_preserves_repeated_input():
    u, a, v = message("user", "u"), output("a"), message("user", "v")
    trajectories = build(completion("1", [u], [a], instructions="sys"), completion("2", [u, a, v], [output("b")], previous="1"))
    assert list(export_trajectories(trajectories))[0]["messages"] == [u, message("assistant", "a"), u, message("assistant", "a"), v, message("assistant", "b")]
    assert list(export_codex_samples(trajectories)) == []


def test_repeated_incremental_user_message_is_not_deduplicated():
    u = message("user", "again")
    trajectories = build(completion("1", [u], [output("a")]), completion("2", [u], [output("b")], previous="1"))
    assert list(export_trajectories(trajectories))[0]["messages"] == [u, message("assistant", "a"), u, message("assistant", "b")]


def test_previous_id_forks_from_exact_parent_snapshot():
    u = message("user", "root")
    trajectories = build(completion("1", [u], [output("a")]), completion("2", [message("user", "left")], [output("b")], previous="1"), completion("3", [message("user", "right")], [output("c")], previous="1"))
    exported = list(export_trajectories(trajectories))
    assert len(exported) == 2
    assert {tuple(row["source_request_ids"]) for row in exported} == {("1", "2"), ("1", "3")}
    right = next(row for row in exported if row["source_request_ids"][-1] == "3")
    assert right["messages"] == [u, message("assistant", "a"), message("user", "right"), message("assistant", "c")]
    assert len(list(export_samples(trajectories))) == 3


@pytest.mark.parametrize("with_previous", [False, True])
def test_runs_never_merge(with_previous):
    u = message("user", "root")
    trajectories = build(completion("1", [u], [output("a")], run_id="run-1"), completion("2", [u, output("a"), message("user", "next")], [output("b")], run_id="run-2", previous="1" if with_previous else None))
    assert len(trajectories) == 2


def test_equal_prompt_retries_are_disambiguated_by_observed_output():
    u = message("user", "same")
    trajectories = build(completion("1", [u], [output("a")], run_id="run"), completion("2", [u], [output("b")], run_id="run"), completion("3", [u, output("a"), message("user", "next")], [output("c")], run_id="run"))
    assert len(trajectories) == 2
    assert [turn.request_id for turn in trajectories[-1].turns] == ["1", "3"]
    assert trajectories[-1].break_reason is None


def test_no_mutation_of_record_context():
    first = completion("1", [message("user", "one")], [output("a")])
    second = completion("2", [message("user", "two")], [output("b")], previous="1")
    before = deepcopy(second)
    build(first, second)
    assert second == before


def test_parallel_tools_remain_valid_messages_with_linked_results():
    calls = [{"type": "function_call", "call_id": cid, "name": "shell", "arguments": '{}'} for cid in ("c1", "c2")]
    results = [{"type": "function_call_output", "call_id": cid, "output": text} for cid, text in (("c1", "one"), ("c2", "two"))]
    trajectories = build(completion("1", [message("user", "work")], calls), completion("2", results, [output("done")], previous="1"))
    messages = list(export_samples(trajectories))[-1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "tool", "assistant"]
    assert [c["id"] for c in messages[1]["tool_calls"]] == ["c1", "c2"]
    assert [m["tool_call_id"] for m in messages[2:4]] == ["c1", "c2"]


def test_codex_export_is_one_complete_sample_per_trajectory():
    calls = [{"type": "function_call", "call_id": "c1", "name": "shell", "arguments": '{"cmd":"pwd"}'}]
    results = [{"type": "function_call_output", "call_id": "c1", "output": "Chunk ID: x\nOutput: /tmp"}]
    trajectories = build(
        completion("1", [message("user", "work")], calls, tools=[{"type": "function", "name": "shell", "description": "run", "parameters": {"type": "object"}}]),
        completion("2", results, [output("done")], previous="1", tools=[{"type": "function", "name": "shell", "description": "run", "parameters": {"type": "object"}}]),
    )
    rows = list(export_codex_samples(trajectories, task="task-1"))
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {"task", "tools", "messages"}
    assert row["task"] == "task-1"
    assert row["tools"] == '[{"type": "function", "function": {"name": "shell", "description": "run", "parameters": {"type": "object"}}}]'
    assert [(item["role"], item["content"]) for item in row["messages"]] == [
        ("user", "work"),
        ("tool_call", '{"name": "shell", "arguments": {"cmd": "pwd"}}'),
        ("tool_response", "Chunk ID: x\nOutput: /tmp"),
        ("assistant", "done"),
    ]


def test_codex_export_flattens_chat_tool_calls():
    assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "shell", "arguments": '{"cmd":"pwd"}'}}],
    }
    tool = {"role": "tool", "tool_call_id": "c1", "content": "/tmp"}
    trajectories = build(
        completion("1", [message("user", "work")], [assistant], protocol=Protocol.CHAT),
        completion("2", [tool], [message("assistant", "done")], protocol=Protocol.CHAT, previous="1"),
    )
    row = list(export_codex_samples(trajectories))[0]
    assert [(item["role"], item["content"]) for item in row["messages"]] == [
        ("user", "work"),
        ("tool_call", '{"name": "shell", "arguments": {"cmd": "pwd"}}'),
        ("tool_response", "/tmp"),
        ("assistant", "done"),
    ]


def test_codex_export_flattens_responses_custom_tool_calls():
    calls = [{"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": "echo hi"}]
    results = [{"type": "custom_tool_call_output", "call_id": "c1", "output": "hi"}]
    trajectories = build(
        completion("1", [message("user", "work")], calls),
        completion("2", results, [output("done")], previous="1"),
    )
    row = list(export_codex_samples(trajectories))[0]
    assert [(item["role"], item["content"]) for item in row["messages"]] == [
        ("user", "work"),
        ("tool_call", '{"name": "exec", "arguments": "echo hi"}'),
        ("tool_response", "hi"),
        ("assistant", "done"),
    ]


def test_codex_export_normalizes_developer_to_system():
    trajectories = build(completion("1", [{"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "rules"}]}], [output("ok")]))
    row = list(export_codex_samples(trajectories))[0]
    assert [item["role"] for item in row["messages"]] == ["system", "assistant"]


def test_codex_export_preserves_readable_reasoning_as_assistant():
    # Shape observed in the GLM Responses capture: readable content, empty
    # summary, no ciphertext, immediately followed by a function call.
    reasoning = {
        "type": "reasoning", "id": "rs_response_1", "status": "completed",
        "summary": [], "encrypted_content": None,
        "content": [{"type": "reasoning_text", "text": "inspect solver"}],
    }
    call = {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{}"}
    result = {"type": "function_call_output", "call_id": "c1", "output": "solver source"}
    trajectories = build(
        completion("1", [message("user", "work")], [reasoning, call]),
        completion("2", [result], [output("done")], previous="1"),
    )
    row = list(export_codex_samples(trajectories))[0]
    assert [(item["role"], item["content"]) for item in row["messages"]] == [
        ("user", "work"),
        ("assistant", "<think>\ninspect solver\n</think>"),
        ("tool_call", '{"name": "shell", "arguments": {}}'),
        ("tool_response", "solver source"),
        ("assistant", "done"),
    ]


def test_codex_export_prefers_reasoning_body_and_separates_complete_blocks():
    reasoning = {
        "type": "reasoning",
        "content": [
            {"type": "reasoning_text", "text": "inspect solver"},
            {"type": "reasoning_text", "text": "verify bounds"},
        ],
        "summary": [{"type": "summary_text", "text": "shorter duplicate summary"}],
    }
    trajectories = build(completion("1", [message("user", "work")], [reasoning, output("done")]))
    row = list(export_codex_samples(trajectories))[0]
    assert row["messages"][1] == {"role": "assistant", "content": "<think>\ninspect solver\nverify bounds\n</think>"}
    assert len(row["messages"]) == 3


def test_codex_export_uses_summary_when_reasoning_body_is_unavailable():
    reasoning = {
        "type": "reasoning", "encrypted_content": "opaque-ciphertext", "content": [],
        "summary": [
            {"type": "summary_text", "text": "inspect solver"},
            {"type": "summary_text", "text": "verify bounds"},
        ],
    }
    trajectories = build(completion("1", [message("user", "work")], [reasoning, output("done")]))
    row = list(export_codex_samples(trajectories))[0]
    assert [(item["role"], item["content"]) for item in row["messages"]] == [
        ("user", "work"),
        ("assistant", "<think>\ninspect solver\nverify bounds\n</think>"),
        ("assistant", "done"),
    ]


def test_codex_export_does_not_turn_encrypted_reasoning_into_text():
    reasoning = {"type": "reasoning", "encrypted_content": "opaque-ciphertext", "summary": []}
    trajectories = build(completion("1", [message("user", "work")], [reasoning, output("done")]))
    row = list(export_codex_samples(trajectories))[0]
    assert [(item["role"], item["content"]) for item in row["messages"]] == [
        ("user", "work"),
        ("assistant", "done"),
    ]


def test_codex_export_skips_leaf_with_unreturned_tool_call():
    calls = [{"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{}"}]
    trajectories = build(completion("1", [message("user", "work")], calls))
    assert has_unresolved_tool_calls(trajectories[0])
    assert list(export_codex_samples(trajectories)) == []


def test_parallel_tool_results_are_matched_by_call_id_in_internal_messages():
    calls = [{"type": "function_call", "call_id": cid, "name": "shell", "arguments": "{}"} for cid in ("c1", "c2")]
    # The second tool finishes first. The raw/message export keeps that wire
    # order and the IDs needed to associate each result with its call.
    results = [{"type": "function_call_output", "call_id": cid, "output": text} for cid, text in (("c2", "two"), ("c1", "one"))]
    trajectories = build(completion("1", [message("user", "work")], calls), completion("2", results, [output("done")], previous="1"))
    messages = list(export_samples(trajectories))[-1]["messages"]
    assert [m["tool_call_id"] for m in messages[2:4]] == ["c2", "c1"]


def test_interleaved_parallel_results_survive_a_new_model_turn():
    """Late results may follow a new model call without losing causality."""
    calls = [
        {"type": "function_call", "call_id": call_id, "name": "exec", "arguments": "{}"}
        for call_id in ("a", "b", "c")
    ]
    trajectory = build(
        completion("1", [message("user", "inspect")], calls),
        # Only a is available; the model is allowed to emit another call.
        completion(
            "2",
            [{"type": "function_call_output", "call_id": "a", "output": "A"}],
            [{"type": "function_call", "call_id": "d", "name": "exec", "arguments": "{}"}],
            previous="1",
        ),
        # b/c arrive after d was emitted; e is another model call in this turn.
        completion(
            "3",
            [
                {"type": "function_call_output", "call_id": "b", "output": "B"},
                {"type": "function_call_output", "call_id": "c", "output": "C"},
            ],
            [{"type": "function_call", "call_id": "e", "name": "exec", "arguments": "{}"}],
            previous="2",
        ),
        completion(
            "4",
            [
                {"type": "function_call_output", "call_id": "d", "output": "D"},
                {"type": "function_call_output", "call_id": "e", "output": "E"},
            ],
            [output("done")],
            previous="3",
        ),
    )
    exported = list(export_samples(trajectory))[-1]
    messages = exported["messages"]
    assert [item.get("type") or item.get("role") for item in trajectory[0].turns[-1].context + trajectory[0].turns[-1].target] == [
        "user", "function_call", "function_call", "function_call",
        "function_call_output", "function_call", "function_call_output",
        "function_call_output", "function_call", "function_call_output",
        "function_call_output", "message",
    ]
    assert [item["tool_call_id"] for item in messages if item["role"] == "tool"] == ["a", "b", "c", "d", "e"]
    assert not has_unresolved_tool_calls(trajectory[0])


def test_codex_export_preserves_namespace_tools_and_function_options():
    tools = [
        {"type": "function", "name": "shell", "strict": False, "parameters": {"type": "object"}},
        {
            "type": "namespace",
            "name": "agents",
            "description": "Agent operations",
            "tools": [
                {"type": "function", "name": "spawn", "strict": True, "parameters": {"type": "object", "required": ["task"]}},
                {"type": "function", "name": "wait", "parameters": {"type": "object"}},
            ],
        },
        {"type": "web_search", "external_web_access": True},
    ]
    original = deepcopy(tools)
    trajectories = build(completion("1", [message("user", "work")], [output("done")], tools=tools))
    exported = json.loads(list(export_codex_samples(trajectories))[0]["tools"])
    assert exported[0] == {"type": "function", "function": {"name": "shell", "strict": False, "parameters": {"type": "object"}}}
    assert exported[1:] == tools[1:]
    assert tools == original


def test_empty_context_does_not_match_every_request():
    assert len(build(completion("1", [], [output("a")]), completion("2", [message("user", "new")], [output("b")]))) == 2


def test_context_compaction_starts_new_trajectory():
    trajectories = build(completion("1", [message("user", "long task")], [output("a")]), completion("2", [message("user", "summary")], [output("b")]))
    assert len(trajectories) == 2


def test_responses_transport_ids_do_not_break_full_history_prefix():
    first_user = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "u"}]}
    first_answer = {"type": "message", "role": "assistant", "id": "msg_old", "status": "completed", "content": [{"type": "output_text", "text": "a"}]}
    resent_answer = {"type": "message", "role": "assistant", "id": "msg_new", "status": "in_progress", "content": [{"type": "output_text", "text": "a"}]}
    next_user = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "v"}]}
    second_answer = {"type": "message", "role": "assistant", "id": "msg_second_old", "status": "completed", "content": [{"type": "output_text", "text": "b"}]}
    resent_second_answer = {"type": "message", "role": "assistant", "id": "msg_second_new", "status": "in_progress", "content": [{"type": "output_text", "text": "b"}]}
    trajectories = build(
        completion("1", [first_user], [first_answer]),
        completion("2", [first_user, first_answer, next_user], [output("b")]),
        completion("3", [first_user, resent_answer, next_user, second_answer, {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "w"}]}], [output("c")]),
    )
    assert len(trajectories) == 1


def test_prompt_prefix_does_not_consume_parent_when_answer_is_omitted():
    user = message("user", "u")
    trajectories = build(
        completion("1", [user], [output("a")], run_id="run"),
        # Looks like an extension of the parent prompt but does not contain
        # the parent's answer. This must remain an explicit broken root.
        completion("2", [user, message("user", "next")], [output("b")], run_id="run"),
    )
    assert len(trajectories) == 2
    assert trajectories[-1].break_reason == "prompt_prefix_history_mismatch"


def test_prefix_and_previous_response_id_are_isolated_by_session_key():
    user = message("user", "u")
    first = completion("1", [user], [output("a")], run_id="run")
    first.session_key = "thread-a"
    second = completion("2", [user, output("a"), message("user", "v")], [output("b")], run_id="run")
    second.session_key = "thread-b"
    assert len(build(first, second)) == 2

    child = completion("3", [message("user", "w")], [output("c")], run_id="run", previous="1")
    child.session_key = "thread-b"
    assert child.previous_response_id == "1"
    trajectories = build(first, child)
    assert len(trajectories) == 2
    assert trajectories[-1].break_reason == "previous_response_not_found"
