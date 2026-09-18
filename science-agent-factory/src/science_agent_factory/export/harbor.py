"""完整 Harbor Task 与固定槽位批次导出。

模块用途：完整 Harbor Task 与固定槽位批次导出。
主要接口：export_harbor_job、export_batch。
输入：配置、任务制品或状态数据。
输出：校验后的数据或明确异常。
副作用与边界：重建 exports 下明确目标；仅复制冻结的任务、元数据和导出记录，不依赖旧 SAF 格式。
"""

import json
from pathlib import Path

from ..contracts.state import BatchState, JobState
from ..storage.files import copy_artifact, reset_directory
from ..storage.json import write_json
from ..verification.harbor_task import harbor_task_fingerprint, validate_harbor_task
from .sft import export_tool_sft


def export_harbor_job(job_dir: Path) -> Path:
    root = job_dir.resolve()
    state = JobState.model_validate_json((root / "state.json").read_text())
    destination = reset_directory(root / "exports/harbor")
    records = []
    for candidate in state.candidates:
        if not candidate.task_dir:
            continue
        task = (root / candidate.task_dir).resolve()
        metadata = (root / candidate.metadata_dir).resolve()
        if not task.is_relative_to(root) or not metadata.is_relative_to(root):
            raise ValueError("export source escapes Job")
        validate_harbor_task(task, expected_task_id=candidate.candidate_id)
        if harbor_task_fingerprint(task) != candidate.task_sha256:
            raise ValueError("frozen task changed")
        task_path = Path("tasks") / candidate.candidate_id
        metadata_path = Path("metadata") / candidate.candidate_id
        copy_artifact(task, destination / task_path)
        copy_artifact(metadata, destination / metadata_path)
        records.append(
            {
                "task_id": candidate.candidate_id,
                "task_path": task_path.as_posix(),
                "metadata_path": metadata_path.as_posix(),
                "sha256": candidate.task_sha256,
            }
        )
    write_json(
        destination / "manifest.json", {"schema_version": "saf.harbor-export.v2", "tasks": records}
    )
    return destination


def export_batch(storage_root: Path, batch_dir: Path) -> dict:
    state = BatchState.model_validate_json((batch_dir / "batch-state.json").read_text())
    destination = reset_directory(batch_dir / "exports")
    records = []
    samples = []
    all_samples = []
    for slot in state.tasks:
        job = storage_root / "jobs" / slot.job_id
        if not (job / "state.json").is_file():
            continue
        exported = export_harbor_job(job)
        for item in json.loads((exported / "manifest.json").read_text())["tasks"]:
            task_id = item["task_id"]
            copy_artifact(exported / item["task_path"], destination / "tasks" / task_id)
            copy_artifact(exported / item["metadata_path"], destination / "metadata" / task_id)
            records.append(item)
        for selection, target in [("default", samples), ("all", all_samples)]:
            target.extend(
                json.loads(line)
                for line in export_tool_sft(job, selection=selection).read_text().splitlines()
            )
    for name, values in [("samples.jsonl", samples), ("all-attempts.jsonl", all_samples)]:
        (destination / name).write_text(
            "".join(json.dumps(v, ensure_ascii=False) + "\n" for v in values)
        )
    report = {
        "schema_version": "saf.batch-export.v2",
        "tasks": records,
        "samples": len(samples),
        "all_attempts": len(all_samples),
        "failed_seeds": [t.seed_id for t in state.tasks if t.status == "failed"],
    }
    write_json(destination / "manifest.json", report)
    return report
