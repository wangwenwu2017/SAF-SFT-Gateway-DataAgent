"""正式系统的独立 Agent 工作区、制品复制和环境残留检查。

模块用途：为每个 Agent 创建独占目录，显式复制授权输入，轮换不可直接删除的旧输出，并清理可由唯一前缀
证明归属当前 Run 的资源。
主要接口：``WorkspaceManager``、``AgentWorkspace``、``rotate_output`` 和 ``ResourceGuard``。
输入：Job 根目录、Agent 角色、候选/尝试标识、源制品路径、``SAF_RESOURCE_PREFIX``，以及可选 Docker daemon 地址。
输出：带独立 Codex state 目录的工作区描述、归档后的正式文件，以及明确属于当前 Run 的残留资源列表。
副作用与边界：创建、复制或原子移动目录；可在指定 Docker daemon 中删除精确前缀匹配的容器、镜像、数据卷和网络，并
终止命令行含此前缀的 QEMU 进程；不执行全局 prune，不删除无法证明归属的 BuildKit 缓存或其他资源，
也不负责启动环境。
"""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..contracts.enums import AgentRole
from ..storage.files import copy_artifact, reject_symlinks, reset_directory


@dataclass(frozen=True, slots=True)
class AgentWorkspace:
    run_id: str
    resource_prefix: str
    run_dir: Path
    workspace: Path
    event_path: Path
    state_dir: Path | None = None


class WorkspaceManager:
    def __init__(self, job_dir: Path):
        self.job_dir = job_dir.resolve()

    def create(
        self,
        role: AgentRole,
        run_id: str,
        *,
        candidate_id: str,
        stable_builder: bool = False,
    ) -> AgentWorkspace:
        run_dir = self.job_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        workspace = (
            self.job_dir / "candidates" / candidate_id / "builder-workspace"
            if stable_builder
            else run_dir / "workspace"
        )
        if stable_builder:
            workspace.mkdir(parents=True, exist_ok=True)
        else:
            reset_directory(workspace)
        state_dir = (
            self.job_dir / "candidates" / candidate_id / "builder-codex-state"
            if stable_builder
            else run_dir / "codex-state"
        )
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(state_dir, 0o700)
        prefix = f"saf-{run_id}".lower().replace("_", "-")
        return AgentWorkspace(
            run_id=run_id,
            resource_prefix=prefix,
            run_dir=run_dir,
            workspace=workspace,
            event_path=run_dir / "sdk_events.jsonl",
            state_dir=state_dir,
        )

    @staticmethod
    def stage(source: Path, workspace: Path, relative_destination: str) -> Path:
        destination = (workspace / relative_destination).resolve()
        if not destination.is_relative_to(workspace.resolve()):
            raise PermissionError(f"staged input escapes workspace: {relative_destination}")
        copy_artifact(source.resolve(), destination, replace=True)
        return destination

    @staticmethod
    def archive(source: Path, destination: Path) -> Path:
        reject_symlinks(source)
        copy_artifact(source, destination, replace=True)
        return destination

    @staticmethod
    def rotate_output(output: Path, recovery_destination: Path) -> Path:
        """把旧输出整体移到本 Run 恢复区，避免递归删除容器创建的异主文件。"""
        if output.exists() or output.is_symlink():
            recovery_destination.parent.mkdir(parents=True, exist_ok=True)
            if recovery_destination.exists() or recovery_destination.is_symlink():
                raise FileExistsError(recovery_destination)
            os.replace(output, recovery_destination)
        output.mkdir(parents=True, exist_ok=False)
        return output

    @staticmethod
    def resolve_output(workspace: Path, relative: str, *, directory: bool) -> Path:
        target = (workspace / relative).resolve()
        if not target.is_relative_to(workspace.resolve()):
            raise PermissionError(f"Agent output escapes workspace: {relative}")
        reject_symlinks(target)
        valid = target.is_dir() if directory else target.is_file()
        if not valid:
            expected = "directory" if directory else "file"
            raise FileNotFoundError(f"Agent output is not a {expected}: {target}")
        return target


class ResourceGuard:
    """只处理名称或命令行明确包含当前唯一前缀的宿主机资源。"""

    def __init__(self, prefix: str, *, docker_host: str | None = None):
        if not prefix.startswith("saf-") or len(prefix) < 12:
            raise ValueError(f"unsafe resource prefix: {prefix}")
        self.prefix = prefix
        self._docker_environment = os.environ.copy()
        if docker_host is not None:
            self._docker_environment["DOCKER_HOST"] = docker_host

    def cleanup_owned(self) -> list[str]:
        cleaned: list[str] = []
        resources = self._docker_resources()
        removal_commands = {
            "docker_container": ["docker", "rm", "-f"],
            "docker_network": ["docker", "network", "rm"],
            "docker_volume": ["docker", "volume", "rm", "-f"],
            "docker_image": ["docker", "image", "rm", "-f"],
        }
        for kind in (
            "docker_container",
            "docker_network",
            "docker_volume",
            "docker_image",
        ):
            for name in resources[kind]:
                result = subprocess.run(
                    [*removal_commands[kind], name],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                    env=self._docker_environment,
                )
                if result.returncode == 0:
                    cleaned.append(f"{kind}:{name}")
        for pid in self._qemu_processes():
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            cleaned.append(f"process:{pid}")
        return cleaned

    def leftovers(self) -> list[str]:
        resources = self._docker_resources()
        return [
            *(f"{kind}:{name}" for kind, names in resources.items() for name in names),
            *(f"process:{pid}" for pid in self._qemu_processes()),
        ]

    def _docker_resources(self) -> dict[str, list[str]]:
        commands = {
            "docker_container": ["docker", "ps", "-a", "--format", "{{.Names}}"],
            "docker_image": [
                "docker",
                "image",
                "ls",
                "--format",
                "{{.Repository}}:{{.Tag}}",
            ],
            "docker_volume": ["docker", "volume", "ls", "--format", "{{.Name}}"],
            "docker_network": ["docker", "network", "ls", "--format", "{{.Name}}"],
        }
        resources: dict[str, list[str]] = {kind: [] for kind in commands}
        for kind, command in commands.items():
            resources[kind] = self._docker_names(command)
        return resources

    def _docker_names(self, command: list[str]) -> list[str]:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=self._docker_environment,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return []
        if result.returncode != 0:
            return []
        return [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip().startswith(self.prefix)
        ]

    def _qemu_processes(self) -> list[int]:
        matches: list[int] = []
        proc = Path("/proc")
        if not proc.is_dir():
            return matches
        for entry in proc.iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                raw = (entry / "cmdline").read_bytes()
            except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                continue
            command = raw.replace(b"\0", b" ").decode("utf-8", errors="replace")
            executable = command.split(" ", 1)[0]
            if self.prefix in command and any(
                marker in executable
                for marker in ("qemu-system", "cloud-hypervisor", "firecracker")
            ):
                matches.append(int(entry.name))
        return matches
