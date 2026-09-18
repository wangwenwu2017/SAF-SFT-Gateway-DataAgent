#!/usr/bin/env bash
# 模块用途：启动、停止和检查仅供 Science Agent Factory 使用的 rootless Docker（无根 Docker）守护进程。
# 主要接口：``scripts/rootless_docker.sh start|stop|restart|status``。
# 输入：可选 ``.env``、``SAF_DOCKER_HOST``、``SAF_ROOTLESS_DOCKER_ROOT``、当前用户 UID 与用户级 systemd。
# 输出：操作结果、Docker 数据根目录和状态诊断；失败时返回非零退出码并显示最近的服务日志。
# 副作用与边界：只创建项目专用数据目录并管理用户服务 ``saf-rootless-docker.service``；不接触宿主全局
# Docker、其他容器或系统级服务。脚本不配置用户 linger（退出登录后保持服务），也不规避 cgroup 限制。

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"
env_file="${project_root}/.env"
service_name="saf-rootless-docker.service"

if [[ -f "${env_file}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${env_file}"
    set +a
fi

runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
docker_host="${SAF_DOCKER_HOST:-unix://${runtime_dir}/saf-docker.sock}"
docker_root="${SAF_ROOTLESS_DOCKER_ROOT:-${project_root}/var/rootless-docker}"

if [[ "${docker_host}" != unix://* ]]; then
    echo "SAF_DOCKER_HOST must use a unix:// socket: ${docker_host}" >&2
    exit 2
fi

data_root="${docker_root}/data"
exec_root="${docker_root}/exec"
pid_file="${docker_root}/docker.pid"

require_command() {
    local command_name="$1"
    if ! command -v "${command_name}" >/dev/null 2>&1; then
        echo "missing required command: ${command_name}" >&2
        exit 2
    fi
}

docker_ready() {
    DOCKER_HOST="${docker_host}" docker info >/dev/null 2>&1
}

show_status() {
    local service_state
    service_state="$(systemctl --user is-active "${service_name}" 2>/dev/null || true)"
    echo "service=${service_state:-unknown}"
    echo "host=${docker_host}"
    echo "configured_data_root=${data_root}"
    if docker_ready; then
        DOCKER_HOST="${docker_host}" docker info \
            --format 'docker_root={{.DockerRootDir}} driver={{.Driver}} images={{.Images}} containers={{.Containers}} cgroup={{.CgroupDriver}}'
        return 0
    fi
    return 1
}

start_daemon() {
    require_command docker
    require_command dockerd-rootless.sh
    require_command systemctl
    require_command systemd-run

    if docker_ready; then
        show_status
        return 0
    fi

    mkdir -p -- "${runtime_dir}" "${data_root}" "${exec_root}"
    export XDG_RUNTIME_DIR="${runtime_dir}"

    if systemctl --user cat "${service_name}" >/dev/null 2>&1; then
        systemctl --user reset-failed "${service_name}" >/dev/null 2>&1 || true
        systemctl --user start "${service_name}"
    else
        systemd-run --user --collect \
            --unit="${service_name%.service}" \
            --property=Restart=on-failure \
            --property=RestartSec=5s \
            dockerd-rootless.sh \
            --data-root="${data_root}" \
            --exec-root="${exec_root}" \
            --pidfile="${pid_file}" \
            --host="${docker_host}"
    fi

    for _ in $(seq 1 60); do
        if docker_ready; then
            show_status
            return 0
        fi
        sleep 1
    done

    echo "project Docker did not become ready within 60 seconds" >&2
    journalctl --user-unit "${service_name}" --no-pager -n 40 >&2 || true
    return 1
}

stop_daemon() {
    require_command systemctl
    systemctl --user stop "${service_name}"
    echo "stopped ${service_name}; global Docker was not changed"
}

action="${1:-status}"
case "${action}" in
    start)
        start_daemon
        ;;
    stop)
        stop_daemon
        ;;
    restart)
        stop_daemon
        start_daemon
        ;;
    status)
        require_command docker
        require_command systemctl
        show_status
        ;;
    *)
        echo "usage: $0 start|stop|restart|status" >&2
        exit 2
        ;;
esac
