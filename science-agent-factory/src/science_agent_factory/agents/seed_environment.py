"""Seed 科学依赖、构建期数据和辅助服务镜像的预热缓存。

模块用途：Seed 科学依赖、构建期数据和辅助服务镜像的预热缓存。
主要接口：SeedEnvironmentCache、SeedEnvironmentCacheEntry、tree_digest、rewrite_compose_to_cached_services、prefer_ipv4_for_build。
输入：配置、任务制品或状态数据。
输出：校验后的数据或明确异常。
副作用与边界：只构建任务环境，不安装 agent；按摘要复用镜像，可跨 Docker daemon 传输；取消时清理本次进程组。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import socket
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..contracts.models import SeedBundle
from ..contracts.settings import SeedEnvironmentCacheSettings
from ..storage.digests import tree_digest
from ..storage.files import reject_symlinks
from .process import terminate_process_group

_IPV4_MARKER = "# SAF_BUILD_PREFER_IPV4"
_FROM_INSTRUCTION = re.compile(r"^\s*FROM\s+\S+(?:\s+AS\s+\S+)?\s*$", re.IGNORECASE)
_NETWORK_HINT = re.compile(
    r"(?:https?://|snapshot_download|hf_hub_download|git\s+clone|\bcurl\b|\bwget\b|"
    r"\bpip(?:3)?\s+install|\bapt-get\b|\bapk\s+add|\b(?:conda|mamba)\b|install\.packages)",
    re.IGNORECASE,
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def prefer_ipv4_for_build(dockerfile_text: str) -> str:
    """在各 Docker 阶段优先 IPv4，并让 curl 对半断开下载有限重试。"""

    if _IPV4_MARKER in dockerfile_text:
        return dockerfile_text.rstrip() + "\n"
    output: list[str] = []
    for line in dockerfile_text.splitlines():
        output.append(line)
        if _FROM_INSTRUCTION.fullmatch(line):
            # 部分官方工具镜像（例如 ``uv:...`` 和 ``deno:bin-...``）只含
            # 一个静态二进制，没有 shell；在这些阶段插入 RUN 会让整个构建
            # 在执行任何真实步骤前失败。它们通常只是被 COPY 的构建辅助阶段，
            # 不需要网络偏好设置。常规 Linux/Python/Ubuntu 阶段仍照常注入。
            image = line.strip().split()[1].split("@", 1)[0].lower()
            if image.startswith(("ghcr.io/astral-sh/uv", "denoland/deno:bin-")):
                continue
            output.extend(
                (
                    _IPV4_MARKER,
                    (
                        "RUN set -eu; "
                        'if [ -w /etc/gai.conf ] || [ "$(id -u)" = 0 ]; '
                        "then touch /etc/gai.conf && "
                        "grep -q 'precedence ::ffff:0:0/96  100' /etc/gai.conf || "
                        "echo 'precedence ::ffff:0:0/96  100' >> /etc/gai.conf; fi; "
                        'curl_home="${HOME:-/root}"; '
                        'if [ "$(id -u)" = 0 ] || [ -w "$curl_home" ]; then '
                        'mkdir -p "$curl_home"; '
                        "printf '%s\\n' 'retry = 5' 'retry-all-errors' "
                        "'retry-delay = 2' 'connect-timeout = 30' "
                        "'speed-limit = 1024' 'speed-time = 120' "
                        '> "$curl_home/.curlrc"; fi'
                    ),
                )
            )
    return "\n".join(output).rstrip() + "\n"


def _build_host_overrides(dockerfile_text: str) -> tuple[str, ...]:
    """为 Docker 构建显式提供宿主机可解析的 IPv4 主机映射。

    BuildKit 的沙箱 DNS 偶发无法解析 Lean、Rustup、GitHub 或 PyPI 域名，而宿主机
    本身可以解析时，显式 ``--add-host`` 能避免无谓失败。解析失败只跳过该
    主机，不改变 Dockerfile 原有网络行为。
    """

    hosts: set[str] = set()
    for match in re.finditer(r"https?://\S+", dockerfile_text):
        hostname = urlsplit(match.group(0).rstrip("'\"),.;")).hostname
        if hostname:
            hosts.add(hostname)
    # Lean Dockerfiles construct the release URL from the toolchain version,
    # so the domain does not occur as a literal URL in the source text.
    if re.search(r"\b(?:elan|lake)\b|lean-toolchain", dockerfile_text):
        hosts.update(
            {
                "releases.lean-lang.org",
                # Elan currently redirects toolchain archives through the singular host.
                "release.lean-lang.org",
                # Lean release downloads may redirect to GitHub's asset host.
                "release-assets.githubusercontent.com",
            }
        )
    # rustup's installer URL is often literal, but the toolchain manifest and
    # archives are fetched from this separate CDN host.
    if re.search(r"\brustup\b|rust-toolchain", dockerfile_text):
        hosts.add("static.rust-lang.org")
    overrides: list[str] = []
    for hostname in sorted(hosts):
        try:
            address = socket.gethostbyname(hostname)
        except OSError:
            continue
        overrides.append(f"{hostname}:{address}")
    return tuple(overrides)


class SeedEnvironmentCacheEntry(BaseModel):
    """一个 Seed 环境在当前 Docker 宿主机上的构建记录。"""

    model_config = ConfigDict(extra="forbid")

    seed_id: str
    environment_sha256: str
    source_path: str
    source_bytes: int = Field(ge=0)
    image: str
    image_id: str | None = None
    image_size_bytes: int | None = Field(default=None, ge=0)
    service_images: dict[str, str] = Field(default_factory=dict)
    status: Literal["building", "ready", "failed"]
    build_started_at: str
    built_at: str | None = None
    build_log: str
    build_network_required: bool = False
    network_hints: list[str] = Field(default_factory=list)
    error: str | None = None


class SeedEnvironmentRegistry(BaseModel):
    """可原子替换并可在中断后继续构建的环境注册表。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["saf.seed-environment-cache.v2"] = "saf.seed-environment-cache.v2"
    seed_root: str
    updated_at: str
    entries: dict[str, SeedEnvironmentCacheEntry] = Field(default_factory=dict)


def _safe_slug(value: str, *, limit: int = 80) -> str:
    normalized = re.sub(r"[^a-z0-9_.-]+", "-", value.lower()).strip("-.")
    return (normalized or "seed")[:limit].rstrip("-.")


def _directory_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _network_hints(dockerfile: Path) -> list[str]:
    hints: list[str] = []
    for number, line in enumerate(
        dockerfile.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        if _NETWORK_HINT.search(line):
            hints.append(f"Dockerfile:{number}:{line.strip()[:240]}")
    return hints[:32]


def _compose_file(environment: Path) -> Path:
    return next(
        (
            environment / name
            for name in ("docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml")
            if (environment / name).is_file()
        ),
        environment / "docker-compose.yaml",
    )


def _compose_services(environment: Path, image_prefix: str, digest: str) -> dict[str, str]:
    compose = _compose_file(environment)
    if not compose.is_file():
        return {}
    payload = yaml.safe_load(compose.read_text(encoding="utf-8")) or {}
    services = payload.get("services", {}) if isinstance(payload, dict) else {}
    result: dict[str, str] = {}
    if not isinstance(services, dict):
        return result
    for name, spec in services.items():
        if name == "main" or not isinstance(spec, dict) or "build" not in spec:
            continue
        result[str(name)] = f"{image_prefix}:{digest[:16]}-{_safe_slug(str(name), limit=32)}"
    return result


def rewrite_compose_to_cached_services(environment: Path, service_images: dict[str, str]) -> None:
    """把投影副本中的辅助服务构建声明替换为已预热的本地镜像。"""

    if not service_images:
        return
    compose = _compose_file(environment)
    if not compose.is_file():
        raise FileNotFoundError("cache declares sidecars but docker-compose.yaml is missing")
    payload = yaml.safe_load(compose.read_text(encoding="utf-8")) or {}
    services = payload.get("services") if isinstance(payload, dict) else None
    if not isinstance(services, dict):
        raise TypeError("docker-compose.yaml has no services mapping")
    for name, image in service_images.items():
        service = services.get(name)
        if not isinstance(service, dict):
            raise TypeError(f"cached Compose service is missing: {name}")
        service.pop("build", None)
        service["image"] = image
        service["pull_policy"] = "never"
    compose.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


class SeedEnvironmentCache:
    """构建并解析当前 Docker 宿主机上的不可变 Seed 环境。"""

    def __init__(self, settings: SeedEnvironmentCacheSettings):
        self.settings = settings
        self.root = settings.root.resolve() / "v2"
        self.registry_path = self.root / "registry.json"
        self._save_lock = asyncio.Lock()
        self._transfer_lock = asyncio.Lock()
        self.rollout_docker_host = (
            settings.rollout_docker_host or os.environ.get("DOCKER_HOST", "").strip() or None
        )
        self.build_docker_host = settings.build_docker_host or self.rollout_docker_host

    @staticmethod
    def _docker_environment(
        docker_host: str | None,
        base: dict[str, str] | None = None,
    ) -> dict[str, str]:
        environment = (base or os.environ).copy()
        if docker_host is None:
            environment.pop("DOCKER_HOST", None)
        else:
            environment["DOCKER_HOST"] = docker_host
        return environment

    def _load(self, seed_root: Path | None = None) -> SeedEnvironmentRegistry:
        if self.registry_path.is_file():
            return SeedEnvironmentRegistry.model_validate_json(
                self.registry_path.read_text(encoding="utf-8")
            )
        return SeedEnvironmentRegistry(
            seed_root=str(seed_root.resolve()) if seed_root else "",
            updated_at=_utc_now(),
        )

    def _save(self, registry: SeedEnvironmentRegistry) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        registry.updated_at = _utc_now()
        temporary = self.registry_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(registry.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.registry_path)

    def resolve(self, seed_id: str, environment_sha256: str) -> SeedEnvironmentCacheEntry | None:
        """按 Seed 与环境摘要解析 ready 镜像；require 模式缺失时显式失败。"""

        if self.settings.mode == "disabled":
            return None
        registry = self._load()
        entry = registry.entries.get(seed_id)
        ready = (
            entry is not None
            and entry.status == "ready"
            and entry.environment_sha256 == environment_sha256
            and entry.image_id is not None
        )
        if ready:
            return entry
        if self.settings.mode == "require":
            raise RuntimeError(
                f"Seed environment cache is not ready for {seed_id} ({environment_sha256[:12]})"
            )
        return None

    def status(self) -> dict[str, Any]:
        """读取任务环境缓存注册表汇总。"""

        registry = self._load()
        counts = Counter(entry.status for entry in registry.entries.values())
        ready = sum(entry.status == "ready" for entry in registry.entries.values())
        return {
            "schema_version": registry.schema_version,
            "seed_root": registry.seed_root,
            "cache_root": str(self.root),
            "total": len(registry.entries),
            "ready": ready,
            "building": counts["building"],
            "failed": counts["failed"],
            "image_bytes": sum(entry.image_size_bytes or 0 for entry in registry.entries.values()),
            "updated_at": registry.updated_at,
            "entries": {
                key: value.model_dump(mode="json")
                for key, value in sorted(registry.entries.items())
            },
        }

    async def available_environment_digests(self) -> dict[str, str]:
        """返回注册表与当前 Docker 镜像存储同时确认可用的 Seed 摘要。"""

        registry = self._load()
        candidates = [
            entry
            for entry in registry.entries.values()
            if entry.status == "ready" and entry.image_id is not None
        ]
        semaphore = asyncio.Semaphore(8)

        async def inspect(
            entry: SeedEnvironmentCacheEntry,
        ) -> tuple[str, str] | None:
            async with semaphore:
                if not await self._image_matches(entry.image, entry.image_id):
                    return None
                for service_image in entry.service_images.values():
                    try:
                        await self._inspect_image(service_image)
                    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
                        return None
                return entry.seed_id, entry.environment_sha256

        inspected = await asyncio.gather(*(inspect(entry) for entry in candidates))
        return dict(item for item in inspected if item is not None)

    async def prewarm(
        self,
        seed_root: Path,
        *,
        seed_ids: set[str] | None = None,
        force: bool = False,
        concurrency: int | None = None,
    ) -> dict[str, Any]:
        """并发预热导入清单中的 Seed；每个条目完成后立即原子保存。"""

        source = seed_root.expanduser().resolve()
        manifest_path = source / "import_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != "saf.seed-import.v1":
            raise ValueError("environment prewarm requires saf.seed-import.v1")
        selected: list[tuple[str, Path]] = []
        for item in manifest.get("entries", []):
            if item.get("status") != "included":
                continue
            seed_id = str(item.get("seed_id", ""))
            if seed_ids is not None and seed_id not in seed_ids:
                continue
            declared = Path(str(item.get("path", "")))
            seed_dir = (source / declared).resolve()
            if (
                not seed_id
                or declared.is_absolute()
                or ".." in declared.parts
                or not seed_dir.is_relative_to(source)
                or not seed_dir.is_dir()
            ):
                raise ValueError(f"unsafe Seed path in import manifest: {declared}")
            reject_symlinks(seed_dir)
            selected.append((seed_id, seed_dir))
        if seed_ids is not None:
            missing = seed_ids - {seed_id for seed_id, _ in selected}
            if missing:
                raise ValueError(f"unknown included Seed IDs: {sorted(missing)}")
        if not selected:
            raise ValueError("environment prewarm selected no included Seed")

        registry = self._load(source)
        if registry.seed_root and Path(registry.seed_root).resolve() != source:
            raise ValueError("cache registry belongs to another Seed root")
        registry.seed_root = str(source)
        self._save(registry)
        semaphore = asyncio.Semaphore(concurrency or self.settings.build_concurrency)

        async def build(seed_id: str, seed_dir: Path) -> None:
            async with semaphore:
                await self._prewarm_one(registry, seed_id, seed_dir, force=force)

        await asyncio.gather(*(build(seed_id, seed_dir) for seed_id, seed_dir in selected))
        return self.status()

    async def _prewarm_one(
        self,
        registry: SeedEnvironmentRegistry,
        seed_id: str,
        seed_dir: Path,
        *,
        force: bool,
    ) -> None:
        seed = SeedBundle.model_validate_json(
            (seed_dir / "seed_bundle.json").read_text(encoding="utf-8")
        )
        seed.validate_files(seed_dir)
        if seed.seed_id != seed_id or seed.environment_dir is None:
            raise ValueError(f"Seed environment metadata mismatch: {seed_id}")
        environment = (seed_dir / seed.environment_dir).resolve()
        if not environment.is_relative_to(seed_dir) or not environment.is_dir():
            raise ValueError(f"Seed environment escapes its bundle: {seed_id}")
        digest = await asyncio.to_thread(tree_digest, environment)
        image = f"{self.settings.image_prefix}:v2-{_safe_slug(seed_id)}-{digest[:16]}"
        existing = registry.entries.get(seed_id)
        if (
            not force
            and existing is not None
            and existing.status == "ready"
            and existing.environment_sha256 == digest
            and await self._image_matches(existing.image, existing.image_id)
        ):
            return
        build_dir = self.root / "builds" / seed_id
        build_dir.mkdir(parents=True, exist_ok=True)
        log_path = build_dir / "build.log"
        log_path.write_text("", encoding="utf-8")
        dockerfile = environment / "Dockerfile"
        if not dockerfile.is_file():
            raise FileNotFoundError(f"Seed environment has no Dockerfile: {seed_id}")
        hints = _network_hints(dockerfile)
        service_images = _compose_services(environment, self.settings.image_prefix, digest)
        entry = SeedEnvironmentCacheEntry(
            seed_id=seed_id,
            environment_sha256=digest,
            source_path=str(environment),
            source_bytes=_directory_bytes(environment),
            image=image,
            service_images=service_images,
            status="building",
            build_started_at=_utc_now(),
            build_log=str(log_path.relative_to(self.root)),
            build_network_required=bool(hints),
            network_hints=hints,
        )
        async with self._save_lock:
            registry.entries[seed_id] = entry
            self._save(registry)
        try:
            if service_images:
                await self._build_services(environment, service_images, digest, log_path)
            runtime_dockerfile = build_dir / "Dockerfile.runtime"
            source_dockerfile = dockerfile.read_text(encoding="utf-8")
            if self.settings.prefer_ipv4:
                source_dockerfile = prefer_ipv4_for_build(source_dockerfile)
            runtime_dockerfile.write_text(source_dockerfile, encoding="utf-8")
            host_overrides = tuple(
                item
                for override in _build_host_overrides(source_dockerfile)
                for item in ("--add-host", override)
            )
            build_command = (
                self.settings.docker_executable,
                "build",
                *(("--network=host",) if self.settings.build_network == "host" else ()),
                *host_overrides,
                "--tag",
                image,
                "--label",
                f"saf.seed_id={seed_id}",
                "--label",
                f"saf.environment_sha256={digest}",
                "--file",
                str(runtime_dockerfile),
                str(environment),
            )
            await self._run_logged(
                build_command,
                cwd=environment,
                log_path=log_path,
                environment=self._docker_environment(self.build_docker_host),
            )
            if self.build_docker_host != self.rollout_docker_host:
                for cached_image in (*service_images.values(), image):
                    await self._transfer_image(cached_image, log_path)
            image_id, image_size = await self._inspect_image(image)
            entry.status = "ready"
            entry.image_id = image_id
            entry.image_size_bytes = image_size
            entry.built_at = _utc_now()
            entry.error = None
        except Exception as exc:  # noqa: BLE001 - 单个 Seed 失败必须落盘并允许其余环境继续
            entry.status = "failed"
            entry.error = f"{type(exc).__name__}: {exc}"
        finally:
            async with self._save_lock:
                registry.entries[seed_id] = entry
                self._save(registry)

    async def _build_services(
        self,
        environment: Path,
        service_images: dict[str, str],
        digest: str,
        log_path: Path,
    ) -> None:
        override = self.root / "builds" / f"compose-{digest[:16]}.yaml"
        # Harbor 的基础 Compose 允许 ``main`` 仅声明 depends_on，由外层
        # 运行器负责注入主镜像；但 `docker compose build <sidecar>` 仍会先
        # 校验所有服务，因而会拒绝这种没有 image/build 的 main。给投影副本
        # 一个永不拉取的占位镜像，只用于通过 Compose 校验，不会参与构建或
        # 运行，也不会改变 Seed 原始文件。
        main_image = f"{self.settings.image_prefix}:{digest[:16]}-main-placeholder"
        cached_services = {
            "main": {
                "image": main_image,
                "pull_policy": "never",
            },
            **{
                name: {"image": image, "pull_policy": "never"}
                for name, image in service_images.items()
            },
        }
        override.write_text(
            yaml.safe_dump(
                {
                    "services": cached_services,
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        process_env = self._docker_environment(self.build_docker_host)
        process_env["CONTEXT_DIR"] = str(environment)
        process_env.setdefault("CPUS", "4")
        process_env.setdefault("MEMORY", "4096M")
        await self._run_logged(
            (
                self.settings.docker_executable,
                "compose",
                "--project-name",
                f"saf-prewarm-{digest[:16]}",
                "--project-directory",
                str(environment),
                "-f",
                str(_compose_file(environment)),
                "-f",
                str(override),
                "build",
                *service_images,
            ),
            cwd=environment,
            log_path=log_path,
            environment=process_env,
        )

    async def _transfer_image(self, image: str, log_path: Path) -> None:
        """把独立 build daemon 的镜像单路导入 rollout daemon。"""

        transfer_dir = self.root / "transfers"
        transfer_dir.mkdir(parents=True, exist_ok=True)
        archive = transfer_dir / f"{hashlib.sha256(image.encode()).hexdigest()}.tar"
        async with self._transfer_lock:
            archive.unlink(missing_ok=True)
            try:
                await self._run_logged(
                    (
                        self.settings.docker_executable,
                        "image",
                        "save",
                        "--output",
                        str(archive),
                        image,
                    ),
                    cwd=transfer_dir,
                    log_path=log_path,
                    environment=self._docker_environment(self.build_docker_host),
                )
                await self._run_logged(
                    (
                        self.settings.docker_executable,
                        "image",
                        "load",
                        "--input",
                        str(archive),
                    ),
                    cwd=transfer_dir,
                    log_path=log_path,
                    environment=self._docker_environment(self.rollout_docker_host),
                )
            finally:
                archive.unlink(missing_ok=True)

    async def _run_logged(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        log_path: Path,
        environment: dict[str, str] | None = None,
    ) -> None:
        with log_path.open("ab") as log:
            log.write(("\n$ " + " ".join(command) + "\n").encode())
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                env=environment,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                return_code = await asyncio.wait_for(
                    process.wait(), timeout=self.settings.build_timeout_s
                )
            except TimeoutError:
                await terminate_process_group(process)
                raise TimeoutError(
                    f"Docker command exceeded {self.settings.build_timeout_s}s"
                ) from None
            except BaseException:
                await terminate_process_group(process)
                raise
        if return_code != 0:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise RuntimeError(f"Docker command exited with {return_code}: {tail}")

    async def _inspect_image(
        self,
        image: str,
        *,
        docker_host: str | None = None,
    ) -> tuple[str, int]:
        process = await asyncio.create_subprocess_exec(
            self.settings.docker_executable,
            "image",
            "inspect",
            image,
            env=self._docker_environment(docker_host or self.rollout_docker_host),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except BaseException:
            await terminate_process_group(process)
            raise
        if process.returncode != 0:
            raise RuntimeError(
                f"docker image inspect failed: {stderr.decode(errors='replace').strip()}"
            )
        payload = json.loads(stdout)
        if not isinstance(payload, list) or len(payload) != 1:
            raise ValueError("docker image inspect returned an unexpected payload")
        image_id = str(payload[0].get("Id", ""))
        image_size = int(payload[0].get("Size", 0))
        if not image_id:
            raise ValueError("docker image inspect returned no image ID")
        return image_id, image_size

    async def _image_matches(
        self,
        image: str,
        expected_id: str | None,
        *,
        docker_host: str | None = None,
    ) -> bool:
        if expected_id is None:
            return False
        try:
            image_id, _ = await self._inspect_image(
                image,
                docker_host=docker_host,
            )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            return False
        return image_id == expected_id


__all__ = [
    "SeedEnvironmentCache",
    "SeedEnvironmentCacheEntry",
    "SeedEnvironmentRegistry",
    "prefer_ipv4_for_build",
    "rewrite_compose_to_cached_services",
    "tree_digest",
]
