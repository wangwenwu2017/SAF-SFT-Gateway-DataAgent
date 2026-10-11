"""Recorded SSE/client replay mismatches, strict linkage and raw-evidence preservation."""
from copy import deepcopy

import pytest
from sft_gateway.app import _build_export, _effective_response
from sft_gateway.export.codex import export_codex_samples
from sft_gateway.protocols.responses_state import ResponsesStreamState
from sft_gateway.trajectory.conversation_index import _canonical_item
from test_conversation_index import completion, message, output
from test_v031_regressions import row


def records():
    """A captured tool call followed by its client-replayed result and final answer."""
    user = message("user", "calculate")
    done = [
        {"id": "reason", "type": "reasoning", "summary": [], "content": [],
         "encrypted_content": "client-cipher", "metadata": {"turn_id": "turn"}},
        {"id": "text", **output("checking"), "phase": "commentary", "metadata": {"turn_id": "turn"}},
        {"id": "tool", "type": "function_call", "call_id": "call", "name": "shell",
         "arguments": '{"cmd":"calculate"}', "metadata": {"turn_id": "turn"}},
    ]
    done[1]["content"][0].update(logprobs=[], annotations=[])
    terminal = deepcopy(done)
    terminal[0]["encrypted_content"] = "terminal-cipher"
    first = row(completion("first", [user], terminal, run_id="task"))
    first["response"]["status"] = "completed"
    first["events"] = [
        {"data": {"type": "response.output_item.done", "output_index": i, "item": item}}
        for i, item in enumerate(done)
    ] + [{"data": {"type": "response.completed", "response": deepcopy(first["response"])}}]
    replay = [
        {"id": "reason", "type": "reasoning", "summary": [], "encrypted_content": "client-cipher"},
        {"id": "text", **output("checking"), "phase": "commentary"},
        {"id": "tool", "type": "function_call", "call_id": "call", "name": "shell",
         "arguments": '{"cmd": "calculate"}'},
    ]
    second = row(completion("second", [user, *replay,
        {"type": "function_call_output", "call_id": "call", "output": "42"}],
        [output("42")], run_id="task"))
    return first, second


def test_terminal_ciphertext_difference_links_from_observed_done_without_mutating_capture():
    first, second = records()
    before = deepcopy([first, second])
    trajectories, stats = _build_export([second, first])
    trajectory, = trajectories
    assert [t.request_id for t in trajectory.turns] == ["first", "second"]
    assert trajectory.turns[0].response_source == "responses_sse_replay"
    assert stats["pending_tool_calls"] == 0
    sample, = export_codex_samples(trajectories)
    assert sample["messages"][-1]["content"] == "42"
    assert [first, second] == before
    assert first["response"]["output"][0]["encrypted_content"] == "terminal-cipher"
    assert trajectory.turns[0].target[0]["encrypted_content"] == "client-cipher"


@pytest.mark.parametrize("change", ["ciphertext", "arguments", "text", "phase", "call_id", "metadata"])
def test_different_replayed_content_cannot_be_joined(change):
    first, second = records()
    items = second["request"]["input"]
    if change == "ciphertext":
        items[1]["encrypted_content"] = "unobserved-cipher"
    elif change == "arguments":
        items[3]["arguments"] = '{"cmd":"different"}'
    elif change == "text":
        items[2]["content"][0]["text"] = "different"
    elif change == "phase":
        items[2]["phase"] = "final_answer"
    elif change == "call_id":
        items[3]["call_id"] = "unobserved-call"
    else:
        items[3]["metadata"] = {"business_state": "different"}
    trajectories, _ = _build_export([first, second])
    assert len(trajectories) == 2
    assert list(export_codex_samples(trajectories)) == []


@pytest.mark.parametrize("change", ["arguments", "cipher_missing", "text", "reordered", "missing_done", "duplicate_done", "late_done", "changed_identity", "missing_identity"])
def test_inconsistent_or_incomplete_sse_is_not_promoted(change):
    first, _ = records()
    events = first["events"]
    terminal = events[-1]["data"]["response"]["output"]
    if change == "arguments":
        terminal[2]["arguments"] = '{"cmd":"other"}'
    elif change == "cipher_missing":
        terminal[0].pop("encrypted_content")
    elif change == "text":
        terminal[1]["content"][0]["text"] = "other"
    elif change == "reordered":
        terminal.reverse()
    elif change == "missing_done":
        events.pop(1)
    elif change == "late_done":
        events.append(events.pop(2))
    elif change == "changed_identity":
        terminal[0]["id"] = "different-item"
    elif change == "missing_identity":
        terminal[0].pop("id")
        events[0]["data"]["item"].pop("id")
    else:
        events.insert(1, deepcopy(events[0]))
    with pytest.raises(ValueError):
        _effective_response(first)
    trajectories, stats = _build_export([first])
    assert not trajectories
    assert stats["record_issues"][0]["reason"] == "invalid_response_events"


def test_terminal_only_and_nonstream_records_remain_usable():
    first, _ = records()
    first["events"] = first["events"][-1:]
    assert _effective_response(first)[0] == first["response"]
    first.pop("events")
    assert _effective_response(first)[0] == first["response"]


def test_normalization_preserves_nonempty_logprobs_and_nested_user_metadata():
    item = {**output("answer"), "metadata": {"turn_id": "wire", "meaning": "keep"}}
    item["content"][0]["logprobs"] = [{"token": "answer"}]
    normalized = _canonical_item(item)
    assert normalized["metadata"] == {"meaning": "keep"}
    assert normalized["content"][0]["logprobs"]
    tool = {"type": "function_call_output", "output": {"metadata": {"turn_id": "business"}}}
    assert _canonical_item(tool) == tool


def test_capture_snapshot_still_uses_advertised_terminal_output():
    first, _ = records()
    state = ResponsesStreamState()
    for event in first["events"]:
        state.consume(event["data"])
    assert state.finalize().response == first["response"]
    assert state.replay_response()["output"][0]["encrypted_content"] == "client-cipher"
    assert state.finalize().response == first["response"]


def test_anonymous_done_item_is_indexed_once_without_changing_capture():
    state = ResponsesStreamState()
    item = output("answer")
    state.consume({"type": "response.output_item.done", "item": item})
    state.consume({"type": "response.completed", "response": {"id": "r", "status": "completed"}})
    assert state.finalize().output_items == [item]
    assert state.replay_response()["output"] == [item]
