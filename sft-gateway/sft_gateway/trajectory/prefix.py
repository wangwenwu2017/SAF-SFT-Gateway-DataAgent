"""保守的消息前缀匹配，思路参考 Polar 的 prompt-only grouping。

输入是当前请求的完整消息列表和按稳定顺序排列的候选消息列表；输出是
唯一的最长严格前缀，或明确的未匹配/歧义原因。比较只做有序 JSON 相等，
不使用响应内容、API key、时间戳、模糊文本相似度或 token 化结果。

这与 Polar 内部基于 token/prompt builder 的分组并不等价：本项目选择消息
级精确匹配，完全相同的 prompt 被视为重试/分支，最长前缀并列时保持断开，
以避免把黑盒 harness 的不同调用错误合成一条 SFT 轨迹。

限制：只比较 prompt 会把“相同 prompt、不同答案”的重试候选判为歧义，即使
后续完整历史含有某个唯一答案。结果是保守断链，不保证所有可重建轮次均被保留。
"""

import json
from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class PrefixMatch:
    """不可变前缀查询结果，承载候选索引、匹配长度和未匹配原因。

    由 find_longest_prompt_prefix 输出；candidate_index 为 None 表示未选定
    唯一候选，prefix_length 是 item 数量而非 token 数量，无 I/O 或链接副作用。
    """
    candidate_index: int | None
    prefix_length: int
    reason: str | None = None


def is_prefix(prefix: Sequence[Any], sequence: Sequence[Any]) -> bool:
    """判断 prefix 是否为 sequence 的有序前缀。

    列表比较遵循 Python 字典相等规则，因此 JSON 对象键顺序不影响结果；
    消息数组的顺序和每个消息的语义字段都会影响结果。
    输入两个序列，返回 bool；允许等长和空前缀，不修改输入。
    """
    return len(prefix) <= len(sequence) and json.dumps(list(prefix), sort_keys=True, ensure_ascii=False) == json.dumps(list(sequence[:len(prefix)]), sort_keys=True, ensure_ascii=False)


def find_longest_prompt_prefix(
    prompt: Sequence[Any], candidate_prompts: Sequence[Sequence[Any]],
) -> PrefixMatch:
    """在 O(N × L) 内寻找唯一最长的非空严格前缀。

    N 是候选数量，L 是候选与当前 prompt 比较的最大长度。调用方必须先按
    run、协议、模型和上下文指纹隔离候选；本函数不认识会话、响应 ID 或
    并发时序，也不会修改候选列表。
    """
    best_length = 0
    matches: list[int] = []
    for index, candidate in enumerate(candidate_prompts):
        length = len(candidate)
        if not 0 < length < len(prompt) or not is_prefix(candidate, prompt):
            continue
        if length > best_length:
            best_length, matches = length, [index]
        elif length == best_length:
            matches.append(index)
    if not matches:
        return PrefixMatch(None, 0, "no_prefix_match")
    if len(matches) > 1:
        return PrefixMatch(None, best_length, "ambiguous_prefix")
    return PrefixMatch(matches[0], best_length)
