"""从已记录的完成请求构建隔离的根到叶历史。

不同于只保存最新 chain tip 的可变映射，每个 response ID 都指向其精确
节点；给旧节点添加子节点会创建分支，不会改写已经捕获的请求。导出时
每个叶节点对应一条独立轨迹，节点在加入索引时复制，避免共享可变 JSON。
比较时仅将顶层 reasoning 的空 encrypted_content 与缺失等同，原始证据保持不变。
"""

import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from ..gateway.detection import Protocol
from .models import CompletedCompletion, Trajectory
from .prefix import is_prefix


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """解析 JSON 键值对；重复键抛 ValueError，避免参数规范化丢失输入。"""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate argument key")
        result[key] = value
    return result


def _canonical_item(value: Any, *, _top_level: bool = True) -> Any:
    """输入 JSON item，返回前缀比较视图；不修改原始证据。
    顶层去传输 id/status，保留业务嵌套字段；function arguments 仅规范化无重复键的合法 JSON。
    custom input 文本、列表次序和类型仍敏感，不能推断任意协议编码的语义等价。"""
    if isinstance(value, dict):
        # ``id`` and ``status`` are transport fields only on a Responses item.
        # Removing them recursively would corrupt user JSON (for example a
        # shell command's object argument containing a legitimate ``status``).
        ignored = {"id", "status", "internal_chat_message_metadata_passthrough"} if _top_level else set()
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in ignored:
                continue
            assistant_item = _top_level and (
                value.get("type") in {"reasoning", "function_call", "custom_tool_call"}
                or value.get("type") == "message" and value.get("role") == "assistant"
            )
            if assistant_item and key == "metadata" and isinstance(item, dict):
                # Codex replays protocol items without the transport turn label.
                # Other metadata remains significant, including nested tool data.
                item = {k: v for k, v in item.items() if not (k == "turn_id" and isinstance(v, str))}
                if not item:
                    continue
            if _top_level and value.get("type") == "reasoning" and key == "content" and item == []:
                continue
            if _top_level and value.get("type") == "reasoning" and key == "encrypted_content" and item is None:
                continue
            if key in {"content", "summary"} and isinstance(item, list):
                # Some Codex reserializations add an empty annotations array to
                # content parts.  It has no semantic effect, unlike arbitrary
                # nested annotations supplied by a user/tool.
                result[key] = [
                    {part_key: part_value for part_key, part_value in part.items()
                     if not (part_value == [] and (part_key == "annotations"
                         or assistant_item and part.get("type") == "output_text" and part_key == "logprobs"))}
                    if isinstance(part, dict) else part
                    for part in item
                ]
            elif key == "arguments" and isinstance(item, str) and (value.get("type") == "function_call" or (not _top_level and "name" in value)):
                try:
                    result[key] = {"$json_arguments": json.loads(item, object_pairs_hook=_unique_object)}
                except (ValueError, TypeError):
                    result[key] = item
            else:
                result[key] = _canonical_item(item, _top_level=False)
        return result
    if isinstance(value, list):
        # Preserve the recursion level.  A top-level list is a list of
        # protocol items (where transport ``id``/``status`` are ignored), but
        # a nested list belongs to user/tool data and must retain those keys.
        return [_canonical_item(item, _top_level=_top_level) for item in value]
    return value


def _context_prefix(prefix: list[dict[str, Any]], sequence: list[dict[str, Any]]) -> bool:
    """输入两个有序 item 列表，输出去传输字段后的前缀布尔值；不修改输入。

    这是有限规范化后的精确 JSON 比较，不是完整 Responses 语义等价判断。
    """
    return is_prefix(_canonical_item(prefix), _canonical_item(sequence))


def _responses_instruction_items(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the normalized Responses instruction preamble for comparisons.

    This deliberately mirrors :class:`ResponsesAdapter` without importing the
    protocol module into the index (which would introduce an adapter cycle).
    Both string and array instruction forms are accepted.  Invalid scalar
    entries are retained as system content so prefix matching never silently
    discards caller input.
    """
    value = request.get("instructions")
    if isinstance(value, str):
        return [{"role": "system", "content": value}] if value else []
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            copied = deepcopy(item)
            if "role" not in copied and "content" not in copied and "text" in copied:
                copied = {"role": "system", "content": [copied]}
            else:
                copied.setdefault("role", "system")
            result.append(copied)
        elif isinstance(item, str):
            result.append({"role": "system", "content": item})
        else:
            result.append({"role": "system", "content": deepcopy(item)})
    return result


@dataclass(eq=False)
class _Node:
    """保存一个 completion、父子引用及断链原因的可变内部节点。

    输入由 ``add`` 提供；该对象仅用于内存索引，不是对外导出接口。
    completion 已在加入前复制，parent/children 则共享索引内的节点引用。
    """
    completion: CompletedCompletion
    parent: "_Node | None" = None
    children: list["_Node"] = field(default_factory=list)
    break_reason: str | None = None


class ConversationIndex:
    """输入完成记录，建立不可改写历史的父子图，输出每个叶的独立轨迹。
    previous_response_id 表示增量输入，instructions 仅本轮生效；无父 ID 时先用完整
    context+target 证据，再选最长候选。调用方批量拓扑排序，在线 add 要求父节点先加入。"""

    def __init__(self, *, allow_unscoped_prefix: bool = True, expand_previous: bool = True) -> None:
        """创建空索引；配置是否允许未分配 run 的消息前缀推断。

        返回 None，只分配内存。网关传 False；离线调用者使用默认 True 时
        必须自行确保输入已按任务分组。本类没有并发写保护，也不做网络或磁盘 I/O。
        """
        self.expand_previous = expand_previous
        self._nodes: list[_Node] = []
        self._responses: dict[tuple[str, str | None, str], list[_Node]] = {}
        # The library can be used with an offline, already partitioned stream
        # where ``unassigned`` is meaningful.  The HTTP gateway passes False:
        # a billing credential is deliberately not a session boundary.
        self.allow_unscoped_prefix = allow_unscoped_prefix

    @staticmethod
    def _compatible(left: CompletedCompletion, right: CompletedCompletion) -> bool:
        """输入两个完成记录，输出是否可参与同一前缀候选集；不修改记录。

        run、原生会话、协议、模型、请求指纹必须全等；相等并不能单独证明父子关系。
        """
        return (
            left.run_id == right.run_id
            and left.session_key == right.session_key
            and left.protocol == right.protocol
            and left.model == right.model
            and left.fingerprint == right.fingerprint
        )

    @staticmethod
    def _linked_context_compatible(left: CompletedCompletion, right: CompletedCompletion) -> bool:
        """输入两个有显式父边的 completion，返回是否具备一致静态训练配置。
        缺省不视为自动继承。返回 False 不删除父边，只使该分支失去完整 SFT 资格。"""
        if left.run_id != right.run_id or left.session_key != right.session_key or left.protocol != right.protocol:
            return False
        return left.model == right.model and all(
            left.request.get(key) == right.request.get(key)
            for key in ("instructions", "tools", "tool_choice", "text", "response_format")
        )

    def add(self, completion: CompletedCompletion) -> Trajectory:
        """深拷贝并加入一轮，返回当前节点的根到节点轨迹。

        输入 completion 必须已经完成协议解析；本方法只负责父节点选择、
        上下文拼接和链接元数据，不发起请求也不做重试。返回值是独立快照，
        后续 add 不会修改该对象。
        """
        turn = deepcopy(completion)
        parent: _Node | None = None
        reason: str | None = None
        if turn.previous_response_id:
            parents = self._responses.get((turn.run_id, turn.session_key, turn.previous_response_id), [])
            if not parents:
                reason = "previous_response_not_found"
            elif len(parents) > 1:
                reason = "ambiguous_response_id"
            else:
                parent = parents[0]
                if not self._linked_context_compatible(parent.completion, turn):
                    reason = "context_fingerprint_mismatch"
                parent_history = parent.completion.context + parent.completion.target
                if not self.expand_previous:
                    pass  # Batch builder already resolved effective contexts.
                elif turn.protocol == Protocol.RESPONSES:
                    old_preamble = _responses_instruction_items(parent.completion.request)
                    new_preamble = _responses_instruction_items(turn.request)
                    # Request instructions are not persisted conversation items.
                    history = parent_history[len(old_preamble):]
                    delta = turn.context[len(new_preamble):]
                    turn.context = deepcopy(new_preamble + history + delta)
                    if old_preamble != new_preamble:
                        reason = "instructions_changed"
                else:
                    turn.context = deepcopy(parent_history + turn.context)
                turn.link_method = "previous_response_id"
        else:
            candidates = [node for node in self._nodes
                          if node.completion.run_id == turn.run_id
                          and node.completion.session_key == turn.session_key
                          and node.completion.protocol == turn.protocol
                          and (turn.run_id != "unassigned" or self.allow_unscoped_prefix)]
            # Use sampled-output evidence BEFORE longest-prefix selection.
            evidence = [node for node in candidates if node.completion.context
                        and _context_prefix(node.completion.context + node.completion.target, turn.context)]
            if evidence:
                longest = max(len(node.completion.context + node.completion.target) for node in evidence)
                best = [node for node in evidence if len(node.completion.context + node.completion.target) == longest]
                # Identical retries are equivalent evidence; raw records remain distinct.
                signatures = {json.dumps(_canonical_item(node.completion.context + node.completion.target), sort_keys=True) + node.completion.fingerprint for node in best}
                if len(signatures) == 1:
                    parent = best[0]
                    turn.prefix_length = longest
                    turn.link_method = "prompt_prefix"
                    if not self._compatible(parent.completion, turn):
                        reason = "context_fingerprint_mismatch"
                else:
                    reason = "ambiguous_prefix"
            elif any(self._compatible(node.completion, turn) and len(node.completion.context) < len(turn.context)
                     and _context_prefix(node.completion.context, turn.context) for node in candidates):
                reason = "prompt_prefix_history_mismatch"

        if parent is not None:
            turn.parent_request_id = parent.completion.request_id
        node = _Node(turn, parent, break_reason=reason)
        if parent is not None:
            parent.children.append(node)
        self._nodes.append(node)
        if turn.response_id:
            self._responses.setdefault((turn.run_id, turn.session_key, turn.response_id), []).append(node)
        return self._trajectory(node)

    @staticmethod
    def _trajectory(leaf: _Node) -> Trajectory:
        """输入内部叶节点，输出沿父链复制的独立 Trajectory；不改变索引。

        最近的断链原因会向叶传播；没有原因不等于历史完整或任务成功。
        """
        path: list[_Node] = []
        node: _Node | None = leaf
        while node is not None:
            path.append(node)
            node = node.parent
        path.reverse()
        first = path[0].completion
        # Stable leaf-derived ID changes when a trajectory is extended. Source
        # request IDs are the stable keys used for deduplicating turn samples.
        return Trajectory(
            trajectory_id=f"traj_{leaf.completion.request_id}",
            run_id=first.run_id,
            protocol=first.protocol,
            model=first.model,
            turns=[deepcopy(item.completion) for item in path],
            break_reason=next((item.break_reason for item in reversed(path) if item.break_reason), None),
        )

    def export(self) -> list[Trajectory]:
        """无需参数，按节点加入顺序返回所有叶路径副本；不做过滤或落盘。

        输出顺序是捕获索引顺序，路径内顺序来自已建立的链接，不能用来推断
        网关没有观察到的工具进程执行时间或跨分支的全局因果顺序。
        """
        return [self._trajectory(node) for node in self._nodes if not node.children]
