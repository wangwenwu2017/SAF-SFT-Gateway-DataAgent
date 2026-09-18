"""Terminal-Bench v3.0.0 到标准 SeedBundle 的一次性边界归一化工具。

模块用途：固定官方 TB3 v3.0.0 提交，下载或读取源码，并为本机可运行的 CPU 任务生成 SeedBundle；
不负责运行 Agent、官方 verifier 或后续批次调度，也不把 TB3 原生格式引入核心包。
主要接口：``prepare_seed_bundles`` 和命令行 ``main``。
输入：固定提交的 Terminal-Bench Git checkout、目标 seed 根目录和可选下载目录。
输出：每个 CPU task 的 ``saf.seed.v2`` 目录，以及使用根目录相对路径记录 70 个导入项和 GPU 排除项的
清单。
副作用与边界：可执行只读 Git 查询/克隆、复制 instruction/environment/solution，并通过受管清单、
同级备份和失败回滚替换目标；明确不复制官方 tests、verifier、cheat 或历史 Agent 轨迹。
"""

from __future__ import annotations

import argparse
import json
import tomllib
import uuid
from pathlib import Path
from typing import Any

from science_agent_factory.contracts.models import SeedBundle, SeedProvenance
from science_agent_factory.storage.files import copy_artifact, remove_path
from science_agent_factory.storage.json import write_json
from science_agent_factory.storage.importing import (
    checkout_pinned_repository,
    replace_managed_import,
    tree_sha256,
)

TB3_VERSION = "v3.0.0"
TB3_COMMIT = "2b0442c3c583b710ca8da14c8e601b99f2f1f244"
TB3_REPOSITORY = "https://github.com/harbor-framework/terminal-bench.git"
EXPECTED_GPU_TASKS = {
    "exam-pdf-eval",
    "fp8-rmsnorm-gemm",
    "jax-speedrun-gpu",
    "math-eval-grader",
}


def _checkout(source: Path) -> Path:
    return checkout_pinned_repository(
        source,
        version=TB3_VERSION,
        repository=TB3_REPOSITORY,
        commit=TB3_COMMIT,
        label="TB3",
    )


def _task_card(task_id: str, instruction: str, metadata: dict[str, Any]) -> dict[str, Any]:
    task = metadata.get("task", {})
    details = metadata.get("metadata", {})
    environment = metadata.get("environment", {})
    artifacts = [str(item) for item in metadata.get("artifacts", [])]
    return {
        "id": f"tb3-v3-{task_id}",
        "title": task.get("name") or task_id,
        "instruction": instruction,
        "taskPrompt": instruction,
        "inputs": ["environment/"],
        "outputs": artifacts,
        "category": details.get("category", "unknown"),
        "subcategory": details.get("subcategory", "unknown"),
        "tags": details.get("tags", []),
        "environment": {
            "type": "docker",
            "dockerfile": "environment/Dockerfile",
            "cpus": int(environment.get("cpus", 1)),
            "memory_mb": int(environment.get("memory_mb", 2048)),
            "storage_mb": int(environment.get("storage_mb", 10_240)),
            "gpus": int(environment.get("gpus", 0)),
            "gpu_types": environment.get("gpu_types", []),
        },
    }


def _prepare_one(task_root: Path, destination: Path) -> dict[str, Any]:
    task_id = task_root.name
    task_toml = task_root / "task.toml"
    instruction_path = task_root / "instruction.md"
    environment = task_root / "environment"
    solution = task_root / "solution"
    for required in (task_toml, instruction_path, environment, solution):
        if not required.exists():
            raise FileNotFoundError(f"TB3 seed input is missing: {required}")
    metadata = tomllib.loads(task_toml.read_text(encoding="utf-8"))
    instruction = instruction_path.read_text(encoding="utf-8")
    card = _task_card(task_id, instruction, metadata)
    if card["environment"]["gpus"]:
        return {
            "source_task_id": task_id,
            "seed_id": f"tb3-v3-{task_id}",
            "status": "excluded",
            "reason": f"requires GPU types {card['environment']['gpu_types']}",
            "category": card["category"],
            "cpus": card["environment"]["cpus"],
            "memory_mb": card["environment"]["memory_mb"],
        }

    destination.mkdir(parents=True)
    copy_artifact(instruction_path, destination / "instruction.md")
    copy_artifact(task_toml, destination / "source_task.toml")
    copy_artifact(environment, destination / "environment")
    copy_artifact(solution, destination / "solution")
    license_path = task_root / "LICENSE.md"
    license_label = "Apache-2.0"
    source_paths = [task_toml, instruction_path, environment, solution]
    if license_path.is_file():
        copy_artifact(license_path, destination / "SOURCE_LICENSE.md")
        source_paths.append(license_path)
        license_label = "See SOURCE_LICENSE.md"
    write_json(destination / "task_card.json", card)
    seed = SeedBundle(
        schema_version="saf.seed.v2",
        seed_id=f"tb3-v3-{task_id}",
        task_card="task_card.json",
        successful_trajectory=None,
        solution_path="solution",
        environment_dir="environment",
        input_dir=None,
        software_dir="environment",
        provenance=SeedProvenance(
            source="terminal-bench",
            source_version=TB3_VERSION,
            source_task_id=task_id,
            source_uri=(
                "https://github.com/harbor-framework/terminal-bench/tree/"
                f"{TB3_VERSION}/tasks/{task_id}"
            ),
            archive_sha256=tree_sha256(source_paths, task_root),
            license=license_label,
        ),
        notes="Imported from official task, environment, and solution; tests were not imported.",
    )
    write_json(destination / "seed_bundle.json", seed)
    seed.validate_files(destination)
    return {
        "source_task_id": task_id,
        "seed_id": seed.seed_id,
        "status": "included",
        "category": card["category"],
        "cpus": card["environment"]["cpus"],
        "memory_mb": card["environment"]["memory_mb"],
        "path": destination.name,
    }


def prepare_seed_bundles(source: Path, destination: Path) -> dict[str, Any]:
    source = _checkout(source.expanduser().resolve())
    destination = destination.expanduser().resolve()
    tasks_root = source / "tasks"
    task_roots = sorted(path for path in tasks_root.iterdir() if (path / "task.toml").is_file())
    if len(task_roots) != 74:
        raise ValueError(f"expected 74 TB3 v3 tasks, found {len(task_roots)}")
    staging = destination.with_name(f".{destination.name}.staging-{uuid.uuid4().hex[:8]}")
    staging.mkdir(parents=True)
    entries: list[dict[str, Any]] = []
    try:
        for task_root in task_roots:
            task_destination = staging / f"tb3-v3-{task_root.name}"
            entry = _prepare_one(task_root, task_destination)
            if entry["status"] == "included":
                entry["path"] = entry["seed_id"]
            entries.append(entry)
        excluded = {item["source_task_id"] for item in entries if item["status"] == "excluded"}
        if excluded != EXPECTED_GPU_TASKS:
            raise ValueError(f"unexpected GPU exclusion set: {sorted(excluded)}")
        manifest = {
            "schema_version": "saf.seed-import.v1",
            "source_version": TB3_VERSION,
            "source_commit": TB3_COMMIT,
            "source_repository": TB3_REPOSITORY,
            "included": sum(item["status"] == "included" for item in entries),
            "excluded": sum(item["status"] == "excluded" for item in entries),
            "entries": entries,
        }
        write_json(staging / "import_manifest.json", manifest)
        replace_managed_import(
            staging,
            destination,
            source=source,
            source_repository=TB3_REPOSITORY,
            source_version=TB3_VERSION,
        )
        return manifest
    finally:
        if staging.exists():
            remove_path(staging)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="准备固定 TB3 v3.0.0 SeedBundle")
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("var/imports/terminal-bench/v3.0.0/source"),
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path("var/seeds/tb3-v3.0.0"),
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    manifest = prepare_seed_bundles(args.source, args.destination)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
