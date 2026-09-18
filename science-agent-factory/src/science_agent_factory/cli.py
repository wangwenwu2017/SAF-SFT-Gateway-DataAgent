"""统一任务、固定槽位批次及环境预热的命令行入口。

模块用途：统一任务、固定槽位批次及环境预热的命令行入口。
主要接口：main。
输入：配置、任务制品或状态数据。
输出：校验后的数据或明确异常。
副作用与边界：run/resume 启动内部 SDK 角色和 Harbor；status 只读；export 重建导出目录；不支持旧 SAF 格式。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from .agents.seed_environment import SeedEnvironmentCache
from .contracts.settings import FactorySettings
from .export import export_harbor_job, export_tool_sft
from .workflow.batch import BatchController
from .workflow.factory import ScienceAgentFactory


def _load_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _settings(path: str | None) -> FactorySettings:
    _load_env(Path.cwd() / ".env")
    if path is None:
        return FactorySettings.load_default()
    return FactorySettings.load(Path(path).expanduser().resolve())


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="saf")
    parser.add_argument("--config", help="自定义 YAML；省略时使用 wheel 内置安全配置")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        item = commands.add_parser(name, help=help_text)
        item.add_argument("--config", default=argparse.SUPPRESS)
        return item

    run = command("run", "从 SeedBundle 启动配置选择的合成工作流")
    run.add_argument("seed_bundle")
    resume = command("resume", "从原子 state.json 的稳定阶段恢复")
    resume.add_argument("job_id")
    status = command("status", "查看 Job 状态")
    status.add_argument("job_id")
    export = command("export", "导出已归档的 Tool-SFT 或 Harbor Tasks")
    export.add_argument("job_id")
    export.add_argument("--format", choices=("tool-sft", "harbor"), default="harbor")
    export.add_argument(
        "--guidance-level",
        choices=(
            "unguided",
            "reference_distilled",
            "seed_guided",
            "all",
        ),
        default="all",
    )
    export.add_argument("--selection", choices=("default", "all"), default="default")
    batch_run = command("batch-run", "从归一化 SeedBundle 根启动可恢复批次")
    batch_run.add_argument("seed_root")
    batch_run.add_argument("--batch-id")
    batch_resume = command("batch-resume", "从原子 batch-state.json 恢复批次")
    batch_resume.add_argument("batch_id")
    batch_resume_generation = command(
        "batch-resume-generation", "只恢复 Builder 任务生成，并在 rollout 前暂停"
    )
    batch_resume_generation.add_argument("batch_id")
    batch_supervise = command("batch-supervise", "同步预热 Seed 环境并调度固定任务与轨迹")
    batch_supervise.add_argument("batch_id")
    batch_rollout = command("batch-rollout", "只采集已 ready 环境的轨迹，不运行 Builder 或环境预热")
    batch_rollout.add_argument("batch_id")
    batch_pause = command("batch-pause", "在运行器停止后把批次归一化为暂停状态")
    batch_pause.add_argument("batch_id")
    batch_status = command("batch-status", "查看批次覆盖率与活动槽位")
    batch_status.add_argument("batch_id")
    batch_status.add_argument("--per-seed", action="store_true")
    batch_export = command("batch-export", "导出精选批次的 Tool-SFT 与 Harbor Tasks")
    batch_export.add_argument("batch_id")
    environment_prewarm = command("environment-prewarm", "预构建 Seed Docker 环境与构建期大型数据")
    environment_prewarm.add_argument("seed_root")
    environment_prewarm.add_argument("--seed-id", action="append", default=[])
    environment_prewarm.add_argument("--concurrency", type=int)
    environment_prewarm.add_argument("--force", action="store_true")
    command("environment-cache-status", "查看 Seed 环境预热注册表")
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        settings = _settings(args.config)
        factory = ScienceAgentFactory(settings)
        if args.command in {
            "run",
            "resume",
            "batch-run",
            "batch-resume",
            "batch-resume-generation",
            "batch-supervise",
            "environment-prewarm",
        }:
            if not settings.allow_host_full_access:
                raise PermissionError(
                    "set allow_host_full_access: true only on a dedicated/disposable worker"
                )
            print(
                "警告：本次 Agent 可直接访问宿主机、Docker、KVM 和网络；"
                f"状态目录为 {settings.storage_root}",
                file=sys.stderr,
            )
        if args.command == "run":
            _print(asyncio.run(factory.run(Path(args.seed_bundle))))
        elif args.command == "resume":
            _print(asyncio.run(factory.resume(args.job_id)))
        elif args.command == "status":
            _print(factory.status(args.job_id))
        elif args.command == "export":
            if args.format == "tool-sft":
                path = export_tool_sft(
                    factory.job_dir(args.job_id),
                    guidance_level=args.guidance_level,
                    selection=args.selection,
                )
            else:
                path = export_harbor_job(factory.job_dir(args.job_id))
            _print(
                {
                    "job_id": args.job_id,
                    "format": args.format,
                    "guidance_level": args.guidance_level,
                    "path": str(path),
                }
            )
        elif args.command == "batch-run":
            controller = BatchController(settings)
            _print(asyncio.run(controller.run(Path(args.seed_root), batch_id=args.batch_id)))
        elif args.command == "batch-resume":
            controller = BatchController(settings)
            _print(asyncio.run(controller.resume(args.batch_id)))
        elif args.command == "batch-resume-generation":
            controller = BatchController(settings)
            _print(asyncio.run(controller.resume_generation(args.batch_id)))
        elif args.command == "batch-supervise":
            _print(asyncio.run(BatchController(settings).supervise(args.batch_id)))
        elif args.command == "batch-rollout":
            cache = (
                SeedEnvironmentCache(settings.seed_environment_cache)
                if settings.seed_environment_cache.mode != "disabled"
                else None
            )
            _print(asyncio.run(BatchController(settings).rollout_only(args.batch_id, cache)))
        elif args.command == "batch-pause":
            _print(BatchController(settings).pause(args.batch_id))
        elif args.command == "batch-status":
            _print(BatchController(settings).status(args.batch_id, per_seed=args.per_seed))
        elif args.command == "batch-export":
            _print(BatchController(settings).export(args.batch_id))
        elif args.command == "environment-prewarm":
            cache = SeedEnvironmentCache(settings.seed_environment_cache)
            _print(
                asyncio.run(
                    cache.prewarm(
                        Path(args.seed_root),
                        seed_ids=set(args.seed_id) if args.seed_id else None,
                        force=args.force,
                        concurrency=args.concurrency,
                    )
                )
            )
        elif args.command == "environment-cache-status":
            _print(SeedEnvironmentCache(settings.seed_environment_cache).status())
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
