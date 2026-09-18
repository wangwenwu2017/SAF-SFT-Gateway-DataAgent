"""完整 Harbor Task 的临时预热镜像引用。

模块用途：完整 Harbor Task 的临时预热镜像引用。
主要接口：prepare_cached_task。
输入：配置、任务制品或状态数据。
输出：校验后的数据或明确异常。
副作用与边界：仅改写 Attempt 内副本，不修改正式任务，不安装或挂载 agent runtime。
"""

import json
from pathlib import Path

from ..storage.files import copy_artifact
from .seed_environment import SeedEnvironmentCacheEntry, rewrite_compose_to_cached_services


def _materialize_cached_seed_environment(
    *, environment: Path, seed_environment: Path, image: str, service_images: dict[str, str]
) -> None:
    """保留 Compose 服务语义；纯 Dockerfile 任务只引用已构建的镜像。"""
    if any(
        (seed_environment / name).is_file()
        for name in ("docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml")
    ):
        copy_artifact(seed_environment, environment)
        rewrite_compose_to_cached_services(environment, {**service_images, "main": image})
    else:
        environment.mkdir(parents=True, exist_ok=False)
        (environment / "Dockerfile").write_text(f"FROM {image}\n")


def _inject_prebuilt_image(task_toml: Path, image: str) -> None:
    """在临时 Harbor Task 配置中声明已预热的主镜像而不改写正式任务。"""

    lines = task_toml.read_text(encoding="utf-8").splitlines()
    environment_index = next(
        (index for index, line in enumerate(lines) if line.strip() == "[environment]"),
        None,
    )
    if environment_index is None:
        lines.extend(("", "[environment]", f"docker_image = {json.dumps(image)}"))
    else:
        next_section = next(
            (
                index
                for index in range(environment_index + 1, len(lines))
                if lines[index].startswith("[")
            ),
            len(lines),
        )
        replaced = False
        for index in range(environment_index + 1, next_section):
            if lines[index].lstrip().startswith("docker_image"):
                lines[index] = f"docker_image = {json.dumps(image)}"
                replaced = True
                break
        if not replaced:
            lines.insert(environment_index + 1, f"docker_image = {json.dumps(image)}")
    task_toml.write_text("\n".join(lines) + "\n", encoding="utf-8")


def prepare_cached_task(source: Path, destination: Path, cache: SeedEnvironmentCacheEntry) -> Path:
    """为 rollout 创建引用预热镜像的副本；Compose 保留源文件及服务语义。"""

    source_environment = (source / "environment").resolve()
    if not source_environment.is_dir():
        raise FileNotFoundError("Harbor seed_copy task has no environment directory")
    destination.mkdir(parents=True, exist_ok=False)
    for item in source.iterdir():
        if item.name != "environment":
            copy_artifact(item, destination / item.name)
    _materialize_cached_seed_environment(
        environment=destination / "environment",
        seed_environment=source_environment,
        image=cache.image,
        service_images=cache.service_images,
    )
    _inject_prebuilt_image(destination / "task.toml", cache.image)
    return destination
