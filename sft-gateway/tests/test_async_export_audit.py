"""并行/异步工具事件的因果校验审计。

异步工具结构回归。输入为构造的 call_id 及跨轮乱序结果；输出为 pending、结构问题和投影顺序断言，不观察真实工具进程时间。
"""

import json

from sft_gateway.export.validation import validate_tool_calls
from sft_gateway.gateway.detection import Protocol
from sft_gateway.export.codex import _chat_messages, _item_messages
from sft_gateway.protocols.responses import ResponsesAdapter


def test_parallel_calls_and_out_of_order_outputs_are_valid_without_reordering():
    items = [
        {"type": "function_call", "call_id": "a", "name": "exec", "arguments": "{}"},
        {"type": "function_call", "call_id": "b", "name": "exec", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "b", "output": "B"},
        {"type": "function_call_output", "call_id": "a", "output": "A"},
    ]
    audit = validate_tool_calls(items, protocol=Protocol.RESPONSES)
    assert audit.complete and audit.has_parallel_calls
    messages = _item_messages(items)
    # Wire completion order is retained; IDs in the compatibility projection
    # make the association recoverable when two calls have the same name.
    assert '"call_id": "b"' in messages[2]["content"]
    assert '"call_id": "a"' in messages[3]["content"]


def test_parallel_batch_keeps_assistant_call_and_result_boundaries():
    items = [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "inspect three files"}]},
        {"type": "function_call", "call_id": "a", "name": "exec", "arguments": "{\"cmd\":\"a\"}"},
        {"type": "function_call", "call_id": "b", "name": "exec", "arguments": "{\"cmd\":\"b\"}"},
        {"type": "function_call", "call_id": "c", "name": "exec", "arguments": "{\"cmd\":\"c\"}"},
        {"type": "function_call_output", "call_id": "b", "output": "B"},
        {"type": "function_call_output", "call_id": "a", "output": "A"},
        {"type": "function_call_output", "call_id": "c", "output": "C"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "continue"}]},
    ]
    messages = _item_messages(items)
    assert [message["role"] for message in messages] == [
        "assistant", "tool_call", "tool_call", "tool_call",
        "tool_response", "tool_response", "tool_response", "assistant",
    ]
    assert [json.loads(messages[index]["content"])["call_id"] for index in (4, 5, 6)] == ["b", "a", "c"]


def test_orphan_missing_duplicate_and_mismatched_outputs_are_incomplete():
    items = [
        {"type": "function_call", "call_id": "a", "name": "exec", "arguments": "{}"},
        {"type": "custom_tool_call_output", "call_id": "a", "output": "wrong type"},
        {"type": "function_call_output", "call_id": "a", "output": "ok"},
        {"type": "function_call_output", "call_id": "a", "output": "duplicate"},
        {"type": "function_call_output", "call_id": "missing", "output": "orphan"},
        {"type": "function_call", "name": "exec", "arguments": "{}"},
    ]
    audit = validate_tool_calls(items, protocol=Protocol.RESPONSES)
    assert not audit.complete
    assert {issue["kind"] for issue in audit.issues} == {
        "tool_type_mismatch", "duplicate_tool_output", "orphan_tool_output", "missing_call_id"
    }


def test_chat_parallel_projection_preserves_ids_only_when_needed():
    items = [
        {"role": "assistant", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "exec", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "exec", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "b", "content": "B"},
        {"role": "tool", "tool_call_id": "a", "content": "A"},
    ]
    audit = validate_tool_calls(items, protocol=Protocol.CHAT)
    assert audit.complete and audit.has_parallel_calls
    messages = _chat_messages(items)
    assert '"call_id": "b"' in messages[1]["content"]


def test_responses_adapter_keeps_custom_tool_call_and_result():
    messages = ResponsesAdapter().items_to_messages([
        {"type": "custom_tool_call", "call_id": "c", "name": "exec", "input": "echo hi"},
        {"type": "custom_tool_call_output", "call_id": "c", "output": "hi"},
    ])
    assert messages[0]["tool_calls"][0]["id"] == "c"
    assert messages[0]["tool_calls"][0]["function"]["arguments"] == "echo hi"
    assert messages[1] == {"role": "tool", "tool_call_id": "c", "content": "hi"}
