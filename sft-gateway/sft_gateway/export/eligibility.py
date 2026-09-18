"""训练资格检查：输入观测轨迹，输出拒绝理由；不修改消息或重新排序工具结果。"""
from __future__ import annotations

from typing import Any

from ..trajectory.models import Trajectory
from .validation import validate_trajectory_tools


def _text_supported(value: Any) -> bool:
    """判断文本内容是否能无遗漏投影；输入 JSON，输出布尔值。"""
    if value is None or isinstance(value, str):
        return True
    return isinstance(value, list) and all(
        isinstance(part, str) or isinstance(part, dict)
        and isinstance(part.get('type'), str)
        and part.get('type') in {'text', 'input_text', 'output_text', 'refusal', 'reasoning_text', 'summary_text'}
        and isinstance(part.get('text', part.get('refusal')), str)
        for part in value
    )


def rejection_reasons(trajectory: Trajectory) -> list[str]:
    """返回确定排序的资格问题；空列表仅代表可作文本 SFT，不代表任务完成或成功。"""
    reasons: set[str] = set()
    if trajectory.break_reason:
        reasons.add(trajectory.break_reason)
    if not trajectory.turns:
        return ['empty_trajectory']
    audit = validate_trajectory_tools(trajectory)
    if not audit.complete:
        reasons.add('unresolved_or_invalid_tools')
    first = trajectory.turns[0]
    # Historical assistant text in an unlinked root has no captured conditioning evidence.
    if any(item.get('role') == 'assistant' or item.get('type') in {'function_call', 'custom_tool_call', 'reasoning'} for item in first.context):
        reasons.add('unobserved_assistant_history')
    for turn in trajectory.turns:
        reasons.update(turn.eligibility_reasons)
        if turn.request.get('n', 1) != 1 or len(turn.response.get('choices') or []) > 1:
            reasons.add('multiple_completion_choices')
        if turn.request.get('conversation'):
            reasons.add('unresolved_server_conversation')
        if turn.request.get('additional_tools'):
            reasons.add('unsupported_additional_tools')
        if turn.fingerprint != first.fingerprint:
            reasons.add('request_conditioning_changed')
        for item in turn.context + turn.target:
            kind = item.get('type')
            if kind in {'function_call', 'custom_tool_call'}:
                continue
            if kind in {'function_call_output', 'custom_tool_call_output'}:
                if not _text_supported(item.get('output')):
                    reasons.add('unsupported_tool_output')
            elif kind == 'reasoning':
                if not _text_supported(item.get('content')) or not _text_supported(item.get('summary')):
                    reasons.add('unsupported_reasoning_content')
            elif kind in {None, 'message'} and item.get('role') in {'system', 'developer', 'user', 'assistant', 'tool'}:
                if not _text_supported(item.get('content')):
                    reasons.add('unsupported_message_content')
                if item.get('_sft_unsupported_fields'):
                    reasons.add('unsupported_chat_extension')
                if item.get('reasoning_content') or item.get('reasoning') or item.get('refusal'):
                    reasons.add('unsupported_chat_extension')
                if item.get('audio') or item.get('function_call'):
                    reasons.add('unsupported_message_extension')
            else:
                reasons.add('unsupported_item:' + str(kind))
    # Every historical supervised item must have an observed generating turn.
    leaf_items = trajectory.turns[-1].context + trajectory.turns[-1].target
    covered: set[int] = set()
    from ..trajectory.conversation_index import _context_prefix
    for turn in trajectory.turns:
        if not _context_prefix(turn.context + turn.target, leaf_items):
            reasons.add('effective_context_not_preserved')
        covered.update(range(len(turn.context), len(turn.context) + len(turn.target)))
    for index, item in enumerate(leaf_items):
        if (item.get('role') == 'assistant' or item.get('type') in {'function_call', 'custom_tool_call', 'reasoning'}) and index not in covered:
            reasons.add('unobserved_assistant_history')
    # Import locally to avoid a cycle: the projector does not call this validator.
    from .codex import _item_messages, _chat_messages
    from ..gateway.detection import Protocol
    leaf = trajectory.turns[-1]
    project = _item_messages if leaf.protocol == Protocol.RESPONSES else _chat_messages
    if not any(message['role'] in {'assistant', 'tool_call'} and message['content'].strip() for message in project(leaf.target)):
        reasons.add('empty_visible_target')
    return sorted(reasons)
