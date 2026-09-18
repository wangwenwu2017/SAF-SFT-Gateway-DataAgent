"""服务器主导 Git 同步守卫的角色、快进和脏工作区测试。

测试目的：验证 Git 同步脚本只允许服务器发布、本地镜像快进拉取，并可靠拒绝未发布或分叉状态。
被测接口：``scripts/git_sync.sh check|publish|pull``。
输入与夹具：临时 bare origin、server/mirror 工作区、仓库级 ``saf.role`` 和不访问网络的假 uv。
预期结果：一致工作区通过；脏状态、角色误用和分叉失败；服务器发布与本地快进最终指向同一 Commit。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
SYNC_SCRIPT = PROJECT_ROOT / "scripts/git_sync.sh"


@dataclass(frozen=True, slots=True)
class GitFixture:
    origin: Path
    server: Path
    mirror: Path
    environment: dict[str, str]


def _run(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=check,
    )


def _git(path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return _run(["git", *arguments], cwd=path)


def _configure_repository(path: Path, *, role: str) -> None:
    _git(path, "config", "user.name", "Science Agent Factory")
    _git(path, "config", "user.email", "science-agent-factory@local")
    _git(path, "config", "saf.role", role)


def _fixture(tmp_path: Path) -> GitFixture:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare")
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")

    server = tmp_path / "server"
    (server / "scripts").mkdir(parents=True)
    shutil.copy2(SYNC_SCRIPT, server / "scripts/git_sync.sh")
    os.chmod(server / "scripts/git_sync.sh", 0o755)
    (server / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(server, "init")
    _git(server, "symbolic-ref", "HEAD", "refs/heads/main")
    _configure_repository(server, role="server")
    _git(server, "add", ".")
    _git(server, "commit", "-m", "baseline")
    _git(server, "remote", "add", "origin", str(origin))
    _git(server, "push", "--set-upstream", "origin", "main")

    mirror = tmp_path / "mirror"
    _run(["git", "clone", str(origin), str(mirror)], cwd=tmp_path)
    _configure_repository(mirror, role="mirror")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    os.chmod(fake_uv, 0o755)
    environment = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    return GitFixture(origin=origin, server=server, mirror=mirror, environment=environment)


def _sync(
    repository: Path,
    command: str,
    fixture: GitFixture,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run(
        [str(repository / "scripts/git_sync.sh"), command],
        cwd=repository,
        environment=fixture.environment,
        check=check,
    )


def test_check_rejects_dirty_or_unpublished_worktrees(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    assert "git_sync_ok role=server" in _sync(fixture.server, "check", fixture).stdout

    (fixture.server / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    dirty = _sync(fixture.server, "check", fixture, check=False)
    assert dirty.returncode != 0
    assert "worktree contains" in dirty.stderr
    (fixture.server / "untracked.txt").unlink()

    (fixture.server / "tracked.txt").write_text("not published\n", encoding="utf-8")
    _git(fixture.server, "add", "tracked.txt")
    _git(fixture.server, "commit", "-m", "not published")
    unpublished = _sync(fixture.server, "check", fixture, check=False)
    assert unpublished.returncode != 0
    assert "does not match origin/main" in unpublished.stderr


def test_server_publish_and_mirror_pull_fast_forward(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    (fixture.server / "tracked.txt").write_text("published\n", encoding="utf-8")
    _git(fixture.server, "add", "tracked.txt")
    _git(fixture.server, "commit", "-m", "publish")

    assert "git_sync_ok role=server" in _sync(fixture.server, "publish", fixture).stdout
    assert "git_sync_ok role=mirror" in _sync(fixture.mirror, "pull", fixture).stdout
    assert (fixture.mirror / "tracked.txt").read_text(encoding="utf-8") == "published\n"
    assert (
        _git(fixture.server, "rev-parse", "HEAD").stdout
        == _git(fixture.mirror, "rev-parse", "HEAD").stdout
    )

    wrong_role = _sync(fixture.mirror, "publish", fixture, check=False)
    assert wrong_role.returncode != 0
    assert "restricted to saf.role=server" in wrong_role.stderr


def test_mirror_pull_rejects_diverged_history(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    (fixture.mirror / "mirror.txt").write_text("local commit\n", encoding="utf-8")
    _git(fixture.mirror, "add", "mirror.txt")
    _git(fixture.mirror, "commit", "-m", "mirror divergence")

    (fixture.server / "server.txt").write_text("server commit\n", encoding="utf-8")
    _git(fixture.server, "add", "server.txt")
    _git(fixture.server, "commit", "-m", "server update")
    _git(fixture.server, "push", "origin", "main")

    result = _sync(fixture.mirror, "pull", fixture, check=False)
    assert result.returncode != 0
    assert "not contained in origin/main" in result.stderr
