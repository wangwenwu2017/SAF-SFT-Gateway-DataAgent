"""Seed Docker 环境预热、注册表和轻量投影测试。

测试目的：确认环境预热只执行一次原始 Docker 构建并记录镜像/数据摘要，缓存命中后 Harbor 直接使用
预构建镜像，多服务 Compose 的辅助构建可替换为固定镜像，IPv4 投影覆盖动态下载域名
并限制 curl 的失活等待，显式 Rollout daemon 优先于进程环境，取消监督任务时不会遗留构建子进程。
被测接口：``SeedEnvironmentCache.prewarm``、``resolve``、``status``、
``available_environment_digests``、
``rewrite_compose_to_cached_services``、``SeedEnvironmentCache._transfer_image``、
``SeedEnvironmentCache._run_logged`` 和
``prepare_cached_task``。
输入与夹具：单 Seed 导入清单、含本地数据的 Docker 环境、模拟 Docker CLI、完整 Harbor Task 和 Compose 文件。
预期结果：注册表使用独立 v2 命名空间且不注入 harness；重复预热跳过 build；投影不复制单容器 Seed 数据
且不再启动 BuildKit；辅助服务不再冷构建；异步取消会终止独立进程组。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from science_agent_factory.agents.seed_environment import (
    SeedEnvironmentCache,
    _build_host_overrides,
    prefer_ipv4_for_build,
    rewrite_compose_to_cached_services,
    tree_digest,
)
from science_agent_factory.agents.task_environment import prepare_cached_task
from science_agent_factory.contracts.settings import SeedEnvironmentCacheSettings
from tests.unit.test_harbor_task import _harbor_task


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def _fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "bin" / "docker"
    calls = tmp_path / "docker-calls.jsonl"
    executable.parent.mkdir()
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

calls = Path(os.environ["FAKE_DOCKER_CALLS"])
with calls.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["image", "inspect"]:
    missing = set(filter(None, os.environ.get("FAKE_DOCKER_MISSING", "").split(",")))
    if sys.argv[-1] in missing:
        print("missing image", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps([{"Id": "sha256:cached-image", "Size": 123456}]))
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, calls


def _seed_root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "seeds"
    seed = root / "seed-one"
    environment = seed / "environment"
    environment.mkdir(parents=True)
    (environment / "Dockerfile").write_text(
        "FROM python:3.11-slim\nCOPY data /opt/data\n", encoding="utf-8"
    )
    (environment / "data").mkdir()
    (environment / "data" / "large.bin").write_bytes(b"science-data")
    (seed / "task_card.json").write_text("{}\n", encoding="utf-8")
    (seed / "solution").mkdir()
    (seed / "solution" / "solve.py").write_text("pass\n", encoding="utf-8")
    _write_json(
        seed / "seed_bundle.json",
        {
            "schema_version": "saf.seed.v2",
            "seed_id": "seed-one",
            "task_card": "task_card.json",
            "successful_trajectory": None,
            "solution_path": "solution",
            "environment_dir": "environment",
            "input_dir": None,
            "software_dir": None,
            "notes": "",
        },
    )
    _write_json(
        root / "import_manifest.json",
        {
            "schema_version": "saf.seed-import.v1",
            "source_version": "test",
            "source_commit": "abc",
            "entries": [
                {
                    "seed_id": "seed-one",
                    "source_task_id": "one",
                    "status": "included",
                    "path": "seed-one",
                }
            ],
        },
    )
    return root, seed


@pytest.mark.asyncio
async def test_prewarm_is_resumable_and_cached_projection_is_thin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker, calls = _fake_docker(tmp_path)
    monkeypatch.setenv("FAKE_DOCKER_CALLS", str(calls))
    root, seed = _seed_root(tmp_path)
    settings = SeedEnvironmentCacheSettings(
        mode="require",
        root=tmp_path / "cache",
        docker_executable=str(docker),
        image_prefix="test/seed",
        build_network="host",
        prefer_ipv4=True,
        build_timeout_s=600,
    )
    cache = SeedEnvironmentCache(settings)

    first = await cache.prewarm(root)
    second = await cache.prewarm(root)

    assert first["ready"] == 1
    assert second["ready"] == 1
    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    assert sum(command and command[0] == "build" for command in commands) == 1
    assert "--network=host" in next(command for command in commands if command[0] == "build")
    runtime_dockerfile = (cache.root / "builds" / "seed-one" / "Dockerfile.runtime").read_text(
        encoding="utf-8"
    )
    assert "SAF_BUILD_PREFER_IPV4" in runtime_dockerfile

    entry = cache.resolve("seed-one", tree_digest(seed / "environment"))
    assert entry is not None
    assert entry.image_id == "sha256:cached-image"
    assert await cache.available_environment_digests() == {
        "seed-one": tree_digest(seed / "environment")
    }
    monkeypatch.setenv("FAKE_DOCKER_MISSING", entry.image)
    assert await cache.available_environment_digests() == {}
    monkeypatch.delenv("FAKE_DOCKER_MISSING")

    task = _harbor_task(tmp_path)
    projected = prepare_cached_task(task, tmp_path / "projected", entry)
    assert not (projected / "environment/data/large.bin").exists()
    assert (
        tomllib.loads((projected / "task.toml").read_text())["environment"]["docker_image"]
        == entry.image
    )
    assert "docker_image" not in (task / "task.toml").read_text()
    assert "claude" not in runtime_dockerfile.lower()


def test_prefer_ipv4_is_idempotent_and_covers_each_build_stage() -> None:
    source = "FROM python:3.12-slim AS build\nRUN true\nFROM ubuntu:24.04\n"

    once = prefer_ipv4_for_build(source)
    twice = prefer_ipv4_for_build(once)

    assert once == twice
    assert once.count("SAF_BUILD_PREFER_IPV4") == 2
    assert "precedence ::ffff:0:0/96  100" in once
    assert "retry-all-errors" in once
    assert "speed-limit = 1024" in once
    assert "speed-time = 120" in once


def test_prefer_ipv4_skips_shell_less_helper_images() -> None:
    source = (
        "FROM ghcr.io/astral-sh/uv:0.11.1 AS uv\n"
        "FROM denoland/deno:bin-2.9.4 AS deno\n"
        "FROM python:3.12-slim\n"
    )

    rewritten = prefer_ipv4_for_build(source)

    assert rewritten.count("SAF_BUILD_PREFER_IPV4") == 1
    assert rewritten.splitlines()[1] == "FROM denoland/deno:bin-2.9.4 AS deno"


def test_build_host_overrides_uses_only_resolvable_url_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    addresses = {
        "release-assets.githubusercontent.com": "192.0.2.12",
        "release.lean-lang.org": "192.0.2.14",
        "releases.lean-lang.org": "192.0.2.10",
        "github.com": "192.0.2.11",
    }
    monkeypatch.setattr(
        "science_agent_factory.agents.seed_environment.socket.gethostbyname",
        lambda host: addresses[host],
    )

    assert _build_host_overrides(
        "RUN elan toolchain install leanprover/lean4:v4.30.0 && git clone https://github.com/x/y"
    ) == (
        "github.com:192.0.2.11",
        "release-assets.githubusercontent.com:192.0.2.12",
        "release.lean-lang.org:192.0.2.14",
        "releases.lean-lang.org:192.0.2.10",
    )


def test_build_host_overrides_adds_rustup_cdn(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(host: str) -> str:
        if host == "static.rust-lang.org":
            return "192.0.2.13"
        raise OSError

    monkeypatch.setattr(
        "science_agent_factory.agents.seed_environment.socket.gethostbyname",
        resolve,
    )

    assert _build_host_overrides("RUN curl https://sh.rustup.rs | sh") == (
        "static.rust-lang.org:192.0.2.13",
    )


def test_rewrite_compose_uses_cached_auxiliary_image(tmp_path: Path) -> None:
    environment = tmp_path / "environment"
    environment.mkdir()
    compose = {
        "services": {
            "main": {"build": "."},
            "sim": {"build": {"context": ".", "dockerfile": "sim/Dockerfile"}},
        }
    }
    (environment / "docker-compose.yaml").write_text(yaml.safe_dump(compose), encoding="utf-8")

    rewrite_compose_to_cached_services(environment, {"sim": "test/seed:abc-sim"})

    rewritten = yaml.safe_load((environment / "docker-compose.yaml").read_text(encoding="utf-8"))
    assert rewritten["services"]["main"]["build"] == "."
    assert "build" not in rewritten["services"]["sim"]
    assert rewritten["services"]["sim"]["image"] == "test/seed:abc-sim"
    assert rewritten["services"]["sim"]["pull_policy"] == "never"


@pytest.mark.asyncio
async def test_cancelling_logged_command_terminates_its_process_group(
    tmp_path: Path,
) -> None:
    settings = SeedEnvironmentCacheSettings(
        root=tmp_path / "cache",
        build_timeout_s=600,
    )
    cache = SeedEnvironmentCache(settings)
    pid_path = tmp_path / "child.pid"
    command = (
        sys.executable,
        "-c",
        (
            "import os,time,pathlib; "
            f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); "
            "time.sleep(600)"
        ),
    )
    running = asyncio.create_task(
        cache._run_logged(command, cwd=tmp_path, log_path=tmp_path / "build.log")
    )
    for _ in range(100):
        if pid_path.is_file():
            break
        await asyncio.sleep(0.01)
    assert pid_path.is_file()
    pid = int(pid_path.read_text(encoding="utf-8"))

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_image_transfer_uses_isolated_build_and_rollout_daemons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "unix:///run/rollout.sock")
    cache = SeedEnvironmentCache(
        SeedEnvironmentCacheSettings(
            root=tmp_path / "cache",
            build_docker_host="unix:///run/build.sock",
        )
    )
    calls: list[tuple[tuple[str, ...], str | None]] = []

    async def fake_run_logged(
        command: tuple[str, ...],
        *,
        cwd: Path,
        log_path: Path,
        environment: dict[str, str] | None = None,
    ) -> None:
        assert cwd == cache.root / "transfers"
        assert log_path == tmp_path / "build.log"
        calls.append((command, (environment or {}).get("DOCKER_HOST")))

    monkeypatch.setattr(cache, "_run_logged", fake_run_logged)

    await cache._transfer_image("test/seed:one", tmp_path / "build.log")

    assert [host for _, host in calls] == [
        "unix:///run/build.sock",
        "unix:///run/rollout.sock",
    ]
    assert calls[0][0][1:3] == ("image", "save")
    assert calls[1][0][1:3] == ("image", "load")
    assert not any((cache.root / "transfers").glob("*.tar"))


def test_explicit_rollout_docker_host_overrides_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "unix:///run/wrong.sock")
    cache = SeedEnvironmentCache(
        SeedEnvironmentCacheSettings(
            root=tmp_path / "cache",
            rollout_docker_host="unix:///run/rollout.sock",
        )
    )

    assert cache.rollout_docker_host == "unix:///run/rollout.sock"


def test_cached_task_preserves_compose_main_semantics(tmp_path):
    from science_agent_factory.agents.seed_environment import SeedEnvironmentCacheEntry

    task = _harbor_task(tmp_path)
    (task / "environment/compose.yaml").write_text(
        "services:\n  main:\n    build: .\n    user: scientist\n    working_dir: /science\n    command: [custom, startup]\n    environment: {MODE: production}\n  database:\n    image: postgres:16\n"
    )
    entry = SeedEnvironmentCacheEntry(
        seed_id="seed",
        environment_sha256="a" * 64,
        source_path=str(task / "environment"),
        source_bytes=1,
        image="cache:v2",
        image_id="sha256:test",
        status="ready",
        build_started_at="now",
        build_log="build.log",
    )
    projected = prepare_cached_task(task, tmp_path / "projection", entry)
    services = yaml.safe_load((projected / "environment/compose.yaml").read_text())["services"]
    assert services["main"]["image"] == "cache:v2"
    assert "build" not in services["main"]
    assert services["main"]["user"] == "scientist"
    assert services["main"]["command"] == ["custom", "startup"]
    assert services["main"]["working_dir"] == "/science"
    assert services["database"]["image"] == "postgres:16"
