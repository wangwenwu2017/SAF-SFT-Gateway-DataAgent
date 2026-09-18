"""Regression tests for trajectory fidelity and SFT eligibility boundaries.

轨迹辅助函数回归。输入为构造 items/finish_reason；输出为字段保留、投影和过滤断言，覆盖顶层 reasoning 空密文字段的有限规范化及原始证据不变。尚未覆盖端到端截断后 DONE、真实非空 broken 样本及 compaction 资格检查。
"""

from sft_gateway.app import _chat_finish_status
from sft_gateway.export.codex import _item_messages, export_codex_samples
from sft_gateway.gateway.detection import Protocol
from sft_gateway.protocols.responses import ResponsesAdapter
from sft_gateway.trajectory.conversation_index import _canonical_item


def test_nested_user_objects_keep_transport_like_keys() -> None:
    item = {"type": "function_call", "arguments": {"rows": [{"id": "user-id", "status": "ok"}]}}
    assert _canonical_item([item])[0]["arguments"]["rows"] == [{"id": "user-id", "status": "ok"}]


def test_responses_instruction_arrays_are_retained_as_system_items() -> None:
    context = ResponsesAdapter().extract_context({
        "instructions": ["first", {"content": "second", "role": "system"}],
        "input": "question",
    })
    assert context == [
        {"role": "system", "content": "first"},
        {"content": "second", "role": "system"},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "question"}]},
    ]


def test_opaque_items_are_not_invented_as_assistant_targets() -> None:
    messages = _item_messages([
        {"type": "response.compaction", "encrypted_content": "ciphertext"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]},
    ])
    assert messages == [{"role": "assistant", "content": "answer"}]


def test_refusal_item_is_exported_as_visible_assistant_text() -> None:
    messages = _item_messages([
        {"type": "message", "role": "assistant", "content": [{"type": "refusal", "refusal": "cannot help"}]},
    ])
    assert messages == [{"role": "assistant", "content": "cannot help"}]


def test_broken_trajectory_is_excluded_unless_explicitly_requested() -> None:
    from sft_gateway.trajectory.models import Trajectory

    trajectory = Trajectory(
        trajectory_id="t",
        run_id="run",
        protocol=Protocol.RESPONSES,
        model="mock",
        turns=[],
        break_reason="previous_response_not_found",
    )
    assert list(export_codex_samples([trajectory])) == []


def test_chat_truncation_reasons_are_incomplete() -> None:
    assert _chat_finish_status("stop") == "completed"
    assert _chat_finish_status("tool_calls") == "completed"
    assert _chat_finish_status("length") == "incomplete"
    assert _chat_finish_status("content_filter") == "incomplete"
    assert _chat_finish_status(None) is None


def test_reasoning_null_ciphertext_matches_absent_without_mutating_evidence() -> None:
    from copy import deepcopy
    from sft_gateway.trajectory.conversation_index import _context_prefix

    item = {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "think"}], "summary": []}
    replay = {**item, "encrypted_content": None}
    original = deepcopy(replay)
    assert _context_prefix([item], [replay])
    assert _context_prefix([replay], [item])
    assert replay == original
    assert not _context_prefix([item], [{**item, "encrypted_content": "cipher"}])
    assert not _context_prefix([{**item, "encrypted_content": "one"}], [{**item, "encrypted_content": "two"}])
    assert not _context_prefix([item], [{**replay, "content": [{"type": "reasoning_text", "text": "changed"}]}])


def test_null_ciphertext_is_not_removed_from_business_objects() -> None:
    from sft_gateway.trajectory.conversation_index import _context_prefix

    nested = {"type": "reasoning", "encrypted_content": None}
    item = {"type": "function_call", "name": "tool", "arguments": {"payload": nested}}
    assert _canonical_item([item])[0]["arguments"]["payload"] == nested
    assert not _context_prefix([{"type": "message"}], [{"type": "message", "encrypted_content": None}])
