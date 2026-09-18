"""Harbor 原生配置、单 Trial 执行与结构化结果读取。

模块用途：把一个完整任务交给外部 Harbor，不解释具体 agent 的认证或协议。
主要接口：HarborRunner.run、freeze_config、load_config、read_trial_result、valid_atif。
输入：原生 Job 配置、任务目录、Attempt 目录、授权指导、可选环境缓存。
输出：配置快照、日志、原始 Harbor 制品及 RolloutResult。
副作用与边界：只在指定目录写文件并启动独立进程组；Harbor 负责安装 harness 和执行 verifier；
超时和取消精确回收进程组。配置环境变量模板不由 SAF 展开；持久结果路径相对 Attempt。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter

from ..contracts.base import PathIdentifier, utc_now
from ..contracts.settings import FactorySettings
from ..contracts.state import RolloutResult
from ..storage.files import copy_artifact, reject_symlinks
from ..storage.json import write_json
from ..verification.harbor_task import validate_harbor_task
from .process import terminate_process_group
from .seed_environment import SeedEnvironmentCache
from .task_environment import prepare_cached_task


def load_config(path: Path, *, audited: bool = False) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("Harbor config must be a native JobConfig mapping")
    agents = payload.get("agents")
    if not isinstance(agents, list) or len(agents) != 1 or not isinstance(agents[0], dict):
        raise ValueError("one SAF attempt requires exactly one Harbor agent configuration")
    if not (agents[0].get("name") or agents[0].get("import_path")):
        raise ValueError("configure the harness in Harbor agents[0], not in SAF")
    if payload.get("install_only") or payload.get("source_jobs"):
        raise ValueError("install-only and regrade jobs cannot produce a new SAF rollout")
    verifier = payload.get("verifier", {})
    if not isinstance(verifier, dict):
        raise TypeError("Harbor verifier must be a mapping")
    if audited and verifier.get("disable", False):
        raise ValueError("audited workflow requires Harbor verification")
    return payload


def freeze_config(source: Path | None, destination: Path, *, audited: bool) -> tuple[str, str]:
    if source is None:
        raise ValueError("harbor.config_file must reference a native Harbor Job configuration")
    source = source.expanduser().resolve()
    load_config(source, audited=audited)
    destination.parent.mkdir(parents=True, exist_ok=True)
    copy_artifact(source, destination)
    return hashlib.sha256(destination.read_bytes()).hexdigest(), str(source.parent)


def valid_atif(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return (
            isinstance(value, dict)
            and str(value.get("schema_version", "")).startswith("ATIF-v")
            and isinstance(value.get("steps"), list)
            and bool(value["steps"])
            and all(isinstance(step, dict) for step in value["steps"])
        )
    except (OSError, ValueError):
        return False


def read_trial_result(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or not payload.get("trial_name")
        or not payload.get("finished_at")
    ):
        raise ValueError("Harbor trial result is missing identity or completion timestamp")
    for key in ("agent_info", "exception_info", "verifier"):
        if payload.get(key) is not None and not isinstance(payload[key], dict):
            raise ValueError(f"invalid Harbor {key}")
    info = payload.get("agent_info") or {}
    if info.get("model_info") is not None and not isinstance(info["model_info"], dict):
        raise ValueError("invalid Harbor model_info")
    for label, value in [
        ("agent", info.get("name")),
        ("model", (info.get("model_info") or {}).get("name")),
    ]:
        if value is not None and not isinstance(value, str):
            raise ValueError(f"invalid Harbor {label} name")
    verifier = payload.get("verifier_result")
    if verifier is not None and not isinstance(verifier, dict):
        raise ValueError("invalid Harbor verifier_result")
    if (
        verifier
        and verifier.get("rewards") is not None
        and not isinstance(verifier["rewards"], dict)
    ):
        raise ValueError("invalid Harbor rewards")
    return payload


def _final_response(path: Path) -> str:
    for step in reversed(json.loads(path.read_text())["steps"]):
        if step.get("source") == "agent" and isinstance(step.get("message"), str):
            return step["message"]
    return ""


class HarborRunner:
    def __init__(self, settings: FactorySettings, *, config_file: Path, config_base_dir: Path):
        self.settings = settings
        self.config_file = config_file
        self.config_base_dir = config_base_dir

    async def run(
        self,
        *,
        task_dir: Path,
        attempt_dir: Path,
        run_id: str,
        guidance: Path | None = None,
        seed_id: str | None = None,
        seed_environment_sha256: str | None = None,
    ) -> RolloutResult:
        TypeAdapter(PathIdentifier).validate_python(run_id)
        reject_symlinks(task_dir)
        source = task_dir.resolve()
        destination = attempt_dir.resolve()
        if (
            source == destination
            or source.is_relative_to(destination)
            or destination.is_relative_to(source)
        ):
            raise ValueError("task and attempt directories must not overlap")
        validate_harbor_task(source)
        config = load_config(
            self.config_file, audited=self.settings.pipeline.workflow_mode == "audited"
        )
        destination.mkdir(parents=True, exist_ok=False)
        started = utc_now()
        result = RolloutResult(outcome="error", started_at=started)
        process = None
        try:
            if seed_id and seed_environment_sha256:
                cache = SeedEnvironmentCache(self.settings.seed_environment_cache).resolve(
                    seed_id, seed_environment_sha256
                )
                if cache is not None:
                    source = prepare_cached_task(source, destination / "task", cache)
            config.update(
                job_name=run_id,
                jobs_dir=str(destination / "harbor"),
                tasks=[{"path": str(source)}],
                datasets=[],
                n_attempts=1,
                n_concurrent_trials=1,
                quiet=True,
            )
            config["retry"] = {**config.get("retry", {}), "max_retries": 0}
            if guidance is not None:
                reject_symlinks(guidance)
                # Copy the context into the attempt so its exact content is archived.
                copy_artifact(guidance, destination / "guidance")
                instructions = destination / "guidance" / "instruction.md"
                extra = list(config.get("extra_instruction_paths", []))
                if instructions.is_file():
                    extra.append(str(instructions))
                config["extra_instruction_paths"] = extra
                assets = destination / "guidance" / "seed"
                if assets.is_dir():
                    environment = config.setdefault("environment", {})
                    mounts = list(environment.get("mounts") or [])
                    if any(m.get("target") == "/opt/saf-guidance" for m in mounts):
                        raise ValueError("Harbor mount conflicts with authorized guidance")
                    mounts.append(
                        {
                            "type": "bind",
                            "source": str(assets),
                            "target": "/opt/saf-guidance",
                            "read_only": True,
                            "bind": {"create_host_path": False},
                        }
                    )
                    environment["mounts"] = mounts
            effective = destination / "harbor-config.yaml"
            effective.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
            environment = os.environ.copy()
            docker_host = self.settings.seed_environment_cache.rollout_docker_host
            if docker_host:
                environment["DOCKER_HOST"] = docker_host
            temporary = destination / "tmp"
            temporary.mkdir()
            environment["TMPDIR"] = str(temporary)
            executable = shutil.which(self.settings.harbor.executable)
            if executable is None:
                raise FileNotFoundError("Harbor executable not found")
            command = [executable, "run", "--config", str(effective), "--yes", "--quiet"]
            with (
                (destination / "stdout.log").open("wb") as stdout,
                (destination / "stderr.log").open("wb") as stderr,
            ):
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=self.config_base_dir,
                    env=environment,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
                try:
                    await asyncio.wait_for(
                        process.wait(), timeout=self.settings.harbor.wall_timeout_s
                    )
                    result.outcome = "completed" if process.returncode == 0 else "error"
                    if process.returncode:
                        result.error = f"Harbor exited with status {process.returncode}"
                except TimeoutError:
                    result.outcome = "timeout"
                    result.error = "Harbor exceeded configured wall_timeout_s"
                    await self._terminate(process)
                result.return_code = process.returncode
            self._read_artifacts(destination, run_id, result)
        except asyncio.CancelledError:
            if process is not None:
                await self._terminate(process)
            result.return_code = process.returncode if process is not None else None
            result.outcome = "cancelled"
            result.error = "rollout cancelled"
            with contextlib.suppress(OSError, ValueError, TypeError):
                self._read_artifacts(destination, run_id, result)
            raise
        except (OSError, ValueError, TypeError) as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            if result.outcome == "completed":
                result.outcome = "error"
        finally:
            result.finished_at = utc_now()
            write_json(destination / "result.json", result)
        return result

    def _read_artifacts(self, root: Path, run_id: str, result: RolloutResult) -> None:
        def invalid(message: str) -> None:
            result.error = f"{result.error}; {message}" if result.error else message
            if result.outcome == "completed":
                result.outcome = "error"

        job = root / "harbor" / run_id
        trials = sorted(job.glob("*/result.json"))
        trajectories = sorted(job.glob("*/agent/trajectory.json"))
        if len(trajectories) == 1 and valid_atif(trajectories[0]):
            copy_artifact(trajectories[0], root / "trajectory.json", replace=True)
            result.trajectory_path = "trajectory.json"
            result.final_response = _final_response(trajectories[0])
        else:
            invalid("Harbor must produce exactly one valid ATIF trajectory")
        if len(trials) != 1:
            invalid("Harbor must produce exactly one completed trial result")
            return
        if len(trajectories) == 1 and trajectories[0].parent.parent != trials[0].parent:
            invalid("Harbor result and trajectory belong to different trials")
            return
        try:
            payload = read_trial_result(trials[0])
            if payload["trial_name"] != trials[0].parent.name:
                raise ValueError("Harbor trial identity differs from its artifact directory")
        except (OSError, ValueError) as exc:
            invalid(str(exc))
            return
        result.trial_result_path = str(trials[0].relative_to(root))
        info = payload.get("agent_info") or {}
        result.agent = info.get("name")
        result.model = (info.get("model_info") or {}).get("name")
        verifier = payload.get("verifier_result")
        result.verification_executed = verifier is not None or bool(
            (payload.get("verifier") or {}).get("started_at")
        )
        result.rewards = verifier.get("rewards") if verifier else None
        if payload.get("exception_info"):
            invalid(json.dumps(payload["exception_info"], ensure_ascii=False))

    async def _terminate(self, process) -> None:
        await terminate_process_group(process, grace_s=self.settings.harbor.shutdown_grace_s)
