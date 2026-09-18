#!/usr/bin/env bash
# 模块用途：从部署根目录加载受限权限的运行配置，并把命令参数原样交给服务器 SAF CLI。
# 主要接口：``scripts/run_server.sh <saf-subcommand> [arguments...]``。
# 输入：项目根目录 Git/origin 状态、``.env``、``configs/server.yaml``、项目与 Harbor 虚拟环境、
# 可选的 ``SAF_DOCKER_HOST`` 及调用方参数。
# 输出：与 ``saf`` 相同的标准输出、标准错误和退出码。
# 副作用与边界：Git 工作区会先做只读一致性检查，再导出运行环境并 exec CLI；可把本进程及其子进程
# 路由到指定 Docker socket，但不启动或修改 Docker；不修改密钥、不自动同步代码。非 Git 发布包跳过 Git 检查。

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"
env_file="${project_root}/.env"
config_file="${project_root}/configs/server.yaml"
saf_bin="${project_root}/.venv/bin/saf"
git_sync="${project_root}/scripts/git_sync.sh"

if [[ -d "${project_root}/.git" ]]; then
    if [[ ! -x "${git_sync}" ]]; then
        echo "missing Git consistency guard: ${git_sync}" >&2
        exit 2
    fi
    "${git_sync}" check
fi

if [[ ! -f "${env_file}" ]]; then
    echo "missing deployment credentials: ${env_file}" >&2
    exit 2
fi
if [[ ! -f "${config_file}" ]]; then
    echo "missing server config: ${config_file}" >&2
    exit 2
fi
if [[ ! -x "${saf_bin}" ]]; then
    echo "missing SAF environment: ${saf_bin}" >&2
    exit 2
fi

set -a
# shellcheck disable=SC1090
source "${env_file}"
set +a

export SAF_DEPLOY_ROOT="${SAF_DEPLOY_ROOT:-${project_root}}"
export SAF_HARBOR_BIN="${SAF_HARBOR_BIN:-${project_root}/.harbor-venv/bin/harbor}"

if [[ -n "${SAF_DOCKER_HOST:-}" ]]; then
    export DOCKER_HOST="${SAF_DOCKER_HOST}"
fi

exec "${saf_bin}" --config "${config_file}" "$@"
