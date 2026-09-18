"""固定 Seed 槽位的统一异步批次调度。

模块用途：每个 Seed 固定一个 Job 和任务数量，生成与采集使用独立并发；失败不换 Seed。
主要接口：BatchController、BatchStateStore。
输入：Seed 导入清单、FactorySettings、Batch ID 与可选预热缓存。
输出：原子 batch-state.json、逐 Seed 状态及统一导出。
副作用与边界：持单写者锁运行独立 Job；暂停不跨进程杀任务；取消时等待所有 worker 回收。
配置快照在批次创建时冻结；环境缺失保留槽位并报告失败，不静默冷构建。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from pathlib import Path

from pydantic import TypeAdapter

from ..agents.harbor import freeze_config
from ..agents.seed_environment import SeedEnvironmentCache
from ..contracts.base import PathIdentifier
from ..contracts.enums import BatchStatus, WorkflowMode
from ..contracts.models import SeedBundle
from ..contracts.settings import FactorySettings
from ..contracts.state import BatchState, BatchTaskState
from ..storage.digests import artifact_digest
from ..storage.paths import resolve_seed_source
from .factory import ScienceAgentFactory, execution_settings_digest
from .persistence import AtomicJsonStore


class BatchStateStore(AtomicJsonStore[BatchState]):
    def __init__(self, root: Path):
        super().__init__(root, "batch-state.json", BatchState)
        self.batch_dir = self.root
        self.lock_path = self.root / ".batch.lock"


class BatchController:
    def __init__(self, settings: FactorySettings, *, factory_type=ScienceAgentFactory):
        self.settings = settings
        self.factory_type = factory_type

    def batch_dir(self, batch_id):
        TypeAdapter(PathIdentifier).validate_python(batch_id)
        root = (self.settings.storage_root / "batches").resolve()
        path = (root / batch_id).resolve()
        if path == root or not path.is_relative_to(root):
            raise ValueError("Batch path escapes storage")
        return path

    async def run(self, seed_root: Path, *, batch_id=None):
        self._access()
        source = seed_root.resolve()
        manifest = json.loads((source / "import_manifest.json").read_text())
        if manifest.get("schema_version") != "saf.seed-import.v1":
            raise ValueError("batch requires saf.seed-import.v1")
        tasks = []
        for entry in manifest["entries"]:
            if entry["status"] == "excluded":
                continue
            if entry["status"] != "included":
                raise ValueError("invalid Seed inclusion status")
            seed_dir = resolve_seed_source(source, entry["path"])
            seed = SeedBundle.model_validate_json((seed_dir / "seed_bundle.json").read_text())
            seed.validate_files(seed_dir)
            if seed.seed_id != entry["seed_id"]:
                raise ValueError("Seed identity differs from import manifest")
            tasks.append(
                BatchTaskState(
                    seed_id=seed.seed_id,
                    seed_dir=str(seed_dir),
                    job_id=f"job-{uuid.uuid4().hex[:16]}",
                )
            )
        if not tasks or len({t.seed_id for t in tasks}) != len(tasks):
            raise ValueError("batch must contain unique included Seeds")
        batch_id = batch_id or f"batch-{uuid.uuid4().hex[:16]}"
        root = self.batch_dir(batch_id)
        root.mkdir(parents=True, exist_ok=False)
        sha, base = freeze_config(
            self.settings.harbor.config_file,
            root / "harbor.yaml",
            audited=self.settings.pipeline.workflow_mode == WorkflowMode.AUDITED,
        )
        state = BatchState(
            batch_id=batch_id,
            seed_root=str(source),
            tasks=tasks,
            config_sha256=sha,
            config_base_dir=base,
            settings_sha256=self._settings_digest(),
        )
        BatchStateStore(root).create(state)
        return await self.resume(batch_id)

    def _settings_digest(self):
        return hashlib.sha256(
            (
                execution_settings_digest(self.settings) + str(self.settings.batch.tasks_per_seed)
            ).encode()
        ).hexdigest()

    def _access(self):
        if not self.settings.allow_host_full_access:
            raise PermissionError("batch requires allow_host_full_access: true")

    async def resume(self, batch_id):
        return await self._run(batch_id)

    async def resume_generation(self, batch_id):
        return await self._run(batch_id, generation_only=True)

    async def rollout_only(self, batch_id, cache=None):
        return await self._run(batch_id, rollout_only=True, cache=cache)

    async def supervise(self, batch_id):
        cache = SeedEnvironmentCache(self.settings.seed_environment_cache)
        if self.settings.seed_environment_cache.mode == "disabled":
            raise ValueError("supervise requires an enabled Seed environment cache")
        return await self._run(batch_id, cache=cache, prewarm=True)

    async def _run(
        self, batch_id, *, generation_only=False, rollout_only=False, cache=None, prewarm=False
    ):
        self._access()
        store = BatchStateStore(self.batch_dir(batch_id))
        with store.lock():
            state = store.load()
            snapshot = self.batch_dir(batch_id) / "harbor.yaml"
            if hashlib.sha256(snapshot.read_bytes()).hexdigest() != state.config_sha256:
                raise ValueError("frozen Harbor config was modified")
            if state.settings_sha256 != self._settings_digest():
                raise ValueError("batch execution settings changed")
            settings = self.settings.model_copy(deep=True)
            settings.harbor.config_file = snapshot
            settings.pipeline.task_count = self.settings.batch.tasks_per_seed
            factory = self.factory_type(settings, config_base_dir=Path(state.config_base_dir))
            generating = asyncio.Semaphore(settings.batch.generation_concurrency)
            collecting = asyncio.Semaphore(max(1, settings.batch.trajectory_concurrency))
            save_lock = asyncio.Lock()
            state.status = BatchStatus.RUNNING
            state.error = None
            store.save(state)
            prewarm_task = (
                asyncio.create_task(cache.prewarm(Path(state.seed_root))) if prewarm else None
            )

            async def save():
                async with save_lock:
                    store.save(state)

            async def environment_ready(task):
                if cache is None:
                    return
                seed_dir = Path(task.seed_dir)
                seed = SeedBundle.model_validate_json((seed_dir / "seed_bundle.json").read_text())
                if seed.environment_dir is None:
                    raise ValueError("environment-gated batch requires Seed environment")
                digest = await asyncio.to_thread(artifact_digest, seed_dir / seed.environment_dir)
                while True:
                    ready = await cache.available_environment_digests()
                    if ready.get(task.seed_id) == digest:
                        return
                    if prewarm_task is None or prewarm_task.done():
                        if prewarm_task is not None:
                            await prewarm_task
                        raise RuntimeError("Seed environment is not ready; fixed slot retained")
                    await asyncio.sleep(settings.batch.environment_poll_s)

            async def worker(task):
                if task.status == "complete":
                    return
                partial = False
                try:
                    await environment_ready(task)
                    if not rollout_only:
                        async with generating:
                            task.status = "generating"
                            await save()
                            exists = (factory.job_dir(task.job_id) / "state.json").is_file()
                            summary = (
                                await factory.resume_generation(task.job_id)
                                if exists
                                else await factory.generate_tasks(
                                    Path(task.seed_dir),
                                    job_id=task.job_id,
                                    candidate_ids=[
                                        f"{task.seed_id}-synth-{i:03d}"
                                        for i in range(1, settings.batch.tasks_per_seed + 1)
                                    ],
                                )
                            )
                            while (
                                summary["status"] not in {"tasks_ready", "curated"}
                                and task.generation_resumes < settings.batch.max_generation_resumes
                            ):
                                task.generation_resumes += 1
                                await save()
                                await asyncio.sleep(settings.batch.generation_resume_backoff_s)
                                summary = await factory.resume_generation(task.job_id)
                            if summary["status"] not in {"tasks_ready", "curated"}:
                                partial = any(
                                    c["stage"] in {"preflight_accepted", "solved", "curated"}
                                    for c in summary.get("candidates", [])
                                )
                                if not partial:
                                    raise RuntimeError(
                                        summary.get("error") or "generation budget exhausted"
                                    )
                            task.error = summary.get("error") if partial else None
                            task.status = "failed" if partial else "ready"
                            if summary["status"] == "curated":
                                task.status = "complete"
                                return
                            await save()
                    elif not (factory.job_dir(task.job_id) / "state.json").is_file():
                        return
                    if generation_only or settings.batch.trajectory_concurrency == 0:
                        return
                    async with collecting:
                        task.status = "collecting"
                        await save()
                        summary = (
                            await factory.collect_tasks(task.job_id)
                            if rollout_only or partial
                            else await factory.resume(task.job_id)
                        )
                        if summary["status"] != "curated":
                            raise RuntimeError(summary.get("error") or "rollout budget exhausted")
                        task.status = "complete"
                        task.error = None
                except asyncio.CancelledError:
                    if task.status == "generating":
                        task.status = "pending"
                    elif task.status == "collecting":
                        task.status = "ready"
                    raise
                except Exception as exc:  # noqa: BLE001 - 保存任务失败，取消异常单独传播
                    task.status = "failed"
                    task.error = f"{type(exc).__name__}: {exc}"
                finally:
                    await save()

            workers = [asyncio.create_task(worker(t)) for t in state.tasks]
            try:
                await asyncio.gather(*workers)
            finally:
                for worker_task in workers:
                    worker_task.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                if prewarm_task is not None:
                    prewarm_task.cancel()
                    await asyncio.gather(prewarm_task, return_exceptions=True)
                state.status = (
                    BatchStatus.CURATED
                    if all(t.status == "complete" for t in state.tasks)
                    else BatchStatus.BLOCKED
                    if any(t.status == "failed" for t in state.tasks)
                    else BatchStatus.PAUSED
                )
                state.error = next((t.error for t in state.tasks if t.status == "failed"), None)
                store.save(state)
            return state.model_dump(mode="json")

    def pause(self, batch_id):
        store = BatchStateStore(self.batch_dir(batch_id))
        with store.lock():
            state = store.load()
            state.status = BatchStatus.PAUSED
            for task in state.tasks:
                if task.status in {"generating", "collecting"}:
                    task.status = "pending"
            store.save(state)
            return state.model_dump(mode="json")

    def status(self, batch_id, *, per_seed=False):
        return BatchStateStore(self.batch_dir(batch_id)).load().model_dump(mode="json")

    def export(self, batch_id):
        from ..export.harbor import export_batch

        return export_batch(self.settings.storage_root, self.batch_dir(batch_id))
