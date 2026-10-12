"""Data Designer 调度独立 SeedBundle 的任务合成。

模块用途：按输入行并行调用原生生成流程，不实现科学审查或独立求解。
主要接口：run_batch、generate_task、main。
输入：JSONL 中的 request_id、seed、config；并发数、输出目录和可选环境文件。
输出：每项原生状态、预审接受的任务路径、结果清单和 Data Designer 数据集。
副作用与边界：创建全新批次目录，调用 SDK/Docker/网络；保留原生审核，停止在 Solver 前；
不覆盖或自动重试旧批次。并发数限制输入行，行内并发由原生配置决定。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

import data_designer.config as dd
from data_designer.config.run_config import RunConfig
from data_designer.interface import DataDesigner
from pydantic import BaseModel

from .cli import _load_env as load_env_file
from .contracts.enums import WorkflowMode
from .contracts.settings import FactorySettings
from .workflow.factory import ScienceAgentFactory

INPUT_FILES = ("seed", "config")


class GeneratorParams(BaseModel):
    output: str


def validate_request(row: dict) -> None:
    if not (Path(row["seed"]) / "seed_bundle.json").is_file():
        raise ValueError("seed must be a SeedBundle directory")
    settings = FactorySettings.load(Path(row["config"]))
    if settings.pipeline.workflow_mode != WorkflowMode.AUDITED:
        raise ValueError("Data Designer task synthesis requires audited mode")
    if not settings.allow_host_full_access:
        raise PermissionError("execution requires allow_host_full_access: true")


@dd.custom_column_generator(required_columns=["request_id", "seed", "config"])
def generate_task(row: dict, generator_params: GeneratorParams) -> dict:
    directory = Path(generator_params.output) / "items" / row["request_id"]
    directory.mkdir(parents=True, exist_ok=False)
    result = {"request_id": row["request_id"], "status": "failed", "task_paths": []}
    try:
        settings = FactorySettings.load(Path(row["config"]))
        settings.storage_root = directory
        factory = ScienceAgentFactory(settings)
        state = asyncio.run(factory.generate_tasks(Path(row["seed"]), job_id="generation"))
        result["native_state"] = state
        result["state_path"] = str(factory.job_dir("generation") / "state.json")
        candidates = state["candidates"]
        if any(candidate.get("attempts") for candidate in candidates):
            raise RuntimeError("generation unexpectedly contains Solver attempts")
        result["task_paths"] = [
            str(factory.job_dir("generation") / candidate["task_dir"])
            for candidate in candidates
            if candidate["stage"] == "preflight_accepted" and candidate.get("task_dir")
        ]
        result["status"] = (
            "completed"
            if candidates and len(result["task_paths"]) == len(candidates)
            else "partial"
        )
    except Exception as exc:  # noqa: BLE001 -- persist native failures per input row
        result["error"] = f"{type(exc).__name__}: {exc}"
    _write_result(directory / "result.json", result)
    return {**row, "task_result": json.dumps(result, ensure_ascii=False)}


def _write_result(path: Path, result: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def run_batch(requests: Path, output: Path, *, concurrency: int = 2) -> list[dict]:
    """Run a new batch; never overwrite or implicitly replay an existing batch."""
    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    rows = [
        json.loads(line)
        for line in requests.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        raise ValueError("requests must contain at least one row")
    identifiers = set()
    for row in rows:
        identifier = row.get("request_id")
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identifier
        ):
            raise ValueError("request_id must be a safe identifier of at most 64 characters")
        if identifier in identifiers:
            raise ValueError("duplicate request_id")
        identifiers.add(identifier)
        for field in INPUT_FILES:
            path = Path(row[field]).expanduser()
            if not path.is_absolute():
                path = requests.resolve().parent / path
            path = path.resolve()
            if not path.exists():
                raise FileNotFoundError(path)
            row[field] = str(path)
        validate_request(row)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    seeds = output / "requests.jsonl"
    seeds.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    builder = dd.DataDesignerConfigBuilder()
    builder.with_seed_dataset(dd.LocalFileSeedSource(path=str(seeds)))
    builder.add_column(
        dd.CustomColumnConfig(
            name="task_result",
            generator_function=generate_task,
            generator_params=GeneratorParams(output=str(output)),
        )
    )
    designer = DataDesigner()
    designer.set_run_config(
        RunConfig(
            buffer_size=1,
            max_concurrent_row_groups=concurrency,
            max_in_flight_tasks=concurrency,
            non_inference_max_parallel_workers=concurrency,
            display_tui=False,
            otel_metrics_port=None,
        )
    )
    generated = designer.create(builder, num_records=len(rows), artifact_path=output / "designer")
    records = generated.load_dataset().to_dict(orient="records")
    if len(records) != len(rows) or {r["request_id"] for r in records} != identifiers:
        raise RuntimeError("Data Designer did not return exactly one result per request")
    results = sorted((json.loads(r["task_result"]) for r in records), key=lambda r: r["request_id"])
    _write_result(output / "results.json", {"results": results})
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    if args.env_file:
        load_env_file(args.env_file)
    results = run_batch(args.requests, args.out, concurrency=args.concurrency)
    print(
        json.dumps(
            {
                "results": str(args.out.resolve() / "results.json"),
                "requests": len(results),
                "failed": sum(r["status"] != "completed" for r in results),
            }
        )
    )
    return int(any(r["status"] != "completed" for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
