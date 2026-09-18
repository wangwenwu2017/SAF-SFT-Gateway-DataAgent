"""版本化 Git Hook 的非交互运行环境测试。

测试目的：验证 pre-push 在服务器非交互 PATH 缺少 uv 时仍能使用账号级 ``.local/bin/uv`` 执行完整门禁。
被测接口：``.githooks/pre-push``。
输入与夹具：临时 Git 工作区、无 uv 的 PATH、假 HOME 和记录调用参数的假 uv 可执行文件。
预期结果：Hook 成功并依次请求模块文档检查、Ruff 和全量 pytest；不存在 uv 时显式失败。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
HOOK = PROJECT_ROOT / ".githooks/pre-push"


def _repository(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    repository = tmp_path / "repository"
    hook = repository / ".githooks/pre-push"
    hook.parent.mkdir(parents=True)
    shutil.copy2(HOOK, hook)
    os.chmod(hook, 0o755)
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)

    fake_home = tmp_path / "home"
    fake_uv = fake_home / ".local/bin/uv"
    fake_uv.parent.mkdir(parents=True)
    log_path = tmp_path / "uv.log"
    fake_uv.write_text(
        '#!/usr/bin/env sh\nprintf "%s\\n" "$*" >> "$SAF_UV_TEST_LOG"\n',
        encoding="utf-8",
    )
    os.chmod(fake_uv, 0o755)
    environment = {
        **os.environ,
        "HOME": str(fake_home),
        "PATH": "/usr/bin:/bin",
        "SAF_UV_TEST_LOG": str(log_path),
    }
    return repository, log_path, environment


def test_pre_push_falls_back_to_account_local_uv(tmp_path: Path) -> None:
    repository, log_path, environment = _repository(tmp_path)

    result = subprocess.run(
        [str(repository / ".githooks/pre-push"), "origin", "unused"],
        cwd=repository,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        "run pytest -q tests/test_module_documentation.py",
        "run ruff check src tests scripts",
        "run pytest -q",
    ]


def test_pre_push_rejects_missing_uv(tmp_path: Path) -> None:
    repository, _, environment = _repository(tmp_path)
    shutil.rmtree(Path(environment["HOME"]) / ".local")

    result = subprocess.run(
        [str(repository / ".githooks/pre-push"), "origin", "unused"],
        cwd=repository,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "pre-push requires uv" in result.stderr
