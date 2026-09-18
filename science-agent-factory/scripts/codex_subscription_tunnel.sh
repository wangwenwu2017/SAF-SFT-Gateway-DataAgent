#!/usr/bin/env bash
# 模块用途：从具备合规 OpenAI 出口的本机建立仅绑定服务器回环地址的 SSH 反向代理隧道。
# 主要接口：scripts/codex_subscription_tunnel.sh [user@server]；前台常驻，退出或收到信号即关闭隧道。
# 输入：可选 SAF_SSH_TARGET、SAF_LOCAL_PROXY_HOST/PORT、SAF_REMOTE_PROXY_PORT 环境变量和 SSH 配置。
# 输出：可供服务器进程使用的 http://127.0.0.1:<remote-port> HTTP CONNECT 代理端点，失败返回非零状态。
# 副作用与边界：启动一个 SSH 进程并占用服务器回环端口；不读取 Codex 凭据、不写项目文件、
# 不把端口暴露到 0.0.0.0，也不负责启动本机代理或在服务器设置环境变量。

set -euo pipefail

target="${1:-${SAF_SSH_TARGET:-}}"
local_proxy_host="${SAF_LOCAL_PROXY_HOST:-127.0.0.1}"
local_proxy_port="${SAF_LOCAL_PROXY_PORT:-7897}"
remote_proxy_port="${SAF_REMOTE_PROXY_PORT:-17897}"

if [[ -z "${target}" ]]; then
    echo "usage: SAF_SSH_TARGET=user@server scripts/codex_subscription_tunnel.sh" >&2
    exit 2
fi

for item in "${local_proxy_port}" "${remote_proxy_port}"; do
    if [[ ! "${item}" =~ ^[0-9]+$ ]] || (( item < 1 || item > 65535 )); then
        echo "proxy ports must be integers between 1 and 65535" >&2
        exit 2
    fi
done

echo "opening Codex subscription proxy on server 127.0.0.1:${remote_proxy_port}" >&2
exec ssh -NT \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=20 \
    -o ServerAliveCountMax=3 \
    -R "127.0.0.1:${remote_proxy_port}:${local_proxy_host}:${local_proxy_port}" \
    "${target}"
