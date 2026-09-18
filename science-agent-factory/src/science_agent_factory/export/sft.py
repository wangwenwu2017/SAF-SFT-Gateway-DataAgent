"""从统一 Attempt 与 ATIF 导出训练记录。

模块用途：从统一 Attempt 与 ATIF 导出训练记录。
主要接口：export_tool_sft。
输入：配置、任务制品或状态数据。
输出：校验后的数据或明确异常。
副作用与边界：只读 Job 和 ATIF，重建精确导出目录；audited 默认只导出接受记录，all 包含失败诊断记录。只导出 ATIF 实际可见推理。
"""

import json
from pathlib import Path
from typing import Any

from ..contracts.state import JobState
from ..storage.files import reset_directory
from ..storage.json import write_json


def _message_text(item: dict[str, Any]) -> str:
    content = item.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for value in content:
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, dict) and isinstance(value.get("text"), str):
                parts.append(value["text"])
        return "\n".join(parts)
    return ""


def _sanitize(value: Any, replacements: list[tuple[str, str]]) -> Any:
    if isinstance(value, str):
        result = value
        for source, target in replacements:
            result = result.replace(source, target)
        return result
    if isinstance(value, list):
        return [_sanitize(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize(item, replacements) for key, item in value.items()}
    return value


def _atif_tool_events(
    trajectory: dict[str, Any],
    replacements: list[tuple[str, str]],
) -> tuple[list[dict[str, Any]], str, str]:
    """把 Harbor ATIF step 转换为消息、推理、工具调用和配对观察。"""

    schema = trajectory.get("schema_version")
    steps = trajectory.get("steps")
    if not str(schema or "").startswith("ATIF-v") or not isinstance(steps, list):
        raise ValueError("Harbor trajectory is not a supported ATIF document")
    converted: list[dict[str, Any]] = []
    user_message = ""
    final_message = ""
    for step in steps:
        if not isinstance(step, dict):
            continue
        source = step.get("source")
        message = _sanitize(_message_text({"content": step.get("message", "")}), replacements)
        copied = bool(step.get("is_copied_context", False))
        if source in {"system", "user"} and message:
            role = str(source)
            if role == "user" and not user_message:
                user_message = message
            converted.append(
                {
                    "kind": "message",
                    "role": role,
                    "content": message,
                    "trainable": False,
                }
            )
            continue
        if source != "agent":
            continue
        reasoning = step.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            converted.append(
                {
                    "kind": "reasoning",
                    "content": _sanitize(reasoning, replacements),
                    "trainable": not copied,
                }
            )
        calls = step.get("tool_calls") if isinstance(step.get("tool_calls"), list) else []
        if message:
            final_message = message
            converted.append(
                {
                    "kind": "message",
                    "role": "assistant",
                    "content": message,
                    "trainable": not copied,
                }
            )
        observations: dict[str, list[dict[str, Any]]] = {}
        observation = step.get("observation")
        if isinstance(observation, dict):
            for item in observation.get("results", []):
                if not isinstance(item, dict):
                    continue
                call_id = item.get("source_call_id")
                if isinstance(call_id, str):
                    observations.setdefault(call_id, []).append(item)
        for call in calls:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("tool_call_id") or "")
            converted.append(
                {
                    "kind": "tool_call",
                    "call_id": call_id,
                    "tool": str(call.get("function_name") or "terminal"),
                    "arguments": _sanitize(call.get("arguments", {}), replacements),
                    "trainable": not copied,
                }
            )
            for item in observations.get(call_id, []):
                converted.append(
                    {
                        "kind": "tool_result",
                        "call_id": call_id,
                        "content": _sanitize(
                            _message_text({"content": item.get("content", "")}),
                            replacements,
                        ),
                        "metadata": _sanitize(item.get("extra"), replacements),
                        "trainable": False,
                    }
                )
    return converted, user_message, final_message


def export_tool_sft(
    job_dir: Path, guidance_level: str = "all", *, selection: str = "default"
) -> Path:
    if selection not in {"default", "all"}:
        raise ValueError("selection must be default or all")
    if guidance_level not in {"all", "unguided", "reference_distilled", "seed_guided"}:
        raise ValueError("invalid guidance level")
    root = job_dir.resolve()
    state = JobState.model_validate_json((root / "state.json").read_text())
    output = (
        reset_directory(root / "exports" / "tool-sft" / selection / guidance_level)
        / "samples.jsonl"
    )
    count = 0
    with output.open("w", encoding="utf-8") as stream:
        for candidate in state.candidates:
            for attempt in candidate.attempts:
                result = attempt.result
                if result.trajectory_path is None:
                    continue
                eligible = bool(
                    attempt.accepted
                    and attempt.reviewed
                    and result.passed
                    and attempt.revision == candidate.revision
                )
                if state.workflow_mode == "audited" and selection == "default" and not eligible:
                    continue
                run = (root / attempt.result_dir).resolve()
                if not run.is_relative_to(root):
                    raise ValueError("attempt escapes Job")
                trajectory_path = (run / result.trajectory_path).resolve()
                if not trajectory_path.is_relative_to(run):
                    raise ValueError("trajectory escapes Attempt")
                trajectory = json.loads(trajectory_path.read_text())
                guidance = json.loads((run / "guidance/manifest.json").read_text())
                level = guidance["effective_level"]
                if guidance_level != "all" and level != guidance_level:
                    continue
                replacements = [(str(root), "<job>"), (str(run), "<attempt>")]
                replacements.sort(key=lambda x: len(x[0]), reverse=True)
                events, user, final = _atif_tool_events(trajectory, replacements)
                record = {
                    "schema_version": "saf.tool-sft.v2",
                    "id": attempt.attempt_id,
                    "job_id": state.job_id,
                    "task_id": candidate.candidate_id,
                    "revision": attempt.revision,
                    "context": {
                        "actual_user_message": user,
                        "guidance_level": level,
                        "guidance_manifest": guidance,
                    },
                    "events": events,
                    "final_response": final or result.final_response,
                    "model": result.model,
                    "agent": result.agent,
                    "outcome": result.outcome,
                    "verification": {
                        "executed": result.verification_executed,
                        "rewards": result.rewards,
                        "passed": result.passed,
                    },
                    "quality": {
                        "workflow_mode": state.workflow_mode.value,
                        "training_eligible": eligible
                        if state.workflow_mode == "audited"
                        else False,
                        "reviewed": attempt.reviewed,
                    },
                    "error": result.error,
                }
                stream.write(json.dumps(_sanitize(record, replacements), ensure_ascii=False) + "\n")
                count += 1
    write_json(output.parent / "manifest.json", {"exported": count, "selection": selection})
    return output
