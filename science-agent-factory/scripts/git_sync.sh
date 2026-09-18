#!/usr/bin/env bash
# 模块用途：让服务器主工作区与本地镜像围绕唯一 origin/main 做受控发布、快进拉取和一致性检查。
# 主要接口：``scripts/git_sync.sh check|publish|pull``；仓库级 ``saf.role`` 取值为 server 或 mirror。
# 输入：当前 Git 工作区、origin/main、仓库级角色配置，以及 pull 模式使用的 uv 锁定环境。
# 输出：成功时输出角色、HEAD 和远端 Commit；失败时输出可操作原因并返回非零状态。
# 副作用与边界：check 只读取状态；publish 推送 main；pull 只做快进合并并同步忽略的虚拟环境；
# 不提交、不变基、不强推、不覆盖脏工作区，也不管理 Seed、轨迹、密钥或其他被忽略制品。

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"

usage() {
    echo "usage: scripts/git_sync.sh check|publish|pull" >&2
    exit 2
}

fail() {
    echo "git sync refused: $*" >&2
    exit 1
}

require_repository() {
    git -C "${project_root}" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
        || fail "${project_root} is not a Git worktree"
}

repository_role() {
    local role
    role="$(git -C "${project_root}" config --local --get saf.role 2>/dev/null || true)"
    case "${role}" in
        server|mirror) printf '%s\n' "${role}" ;;
        *) fail "set repository role with 'git config --local saf.role server|mirror'" ;;
    esac
}

require_main_branch() {
    local branch
    branch="$(git -C "${project_root}" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
    [[ "${branch}" == "main" ]] || fail "current branch must be main, found '${branch:-detached}'"
}

require_clean_worktree() {
    local status
    status="$(git -C "${project_root}" status --porcelain --untracked-files=normal)"
    [[ -z "${status}" ]] || {
        echo "${status}" >&2
        fail "worktree contains tracked or untracked source changes"
    }
}

remote_main_sha() {
    local record
    record="$(git -C "${project_root}" ls-remote --exit-code origin refs/heads/main)" \
        || fail "origin/main is unavailable"
    printf '%s\n' "${record%%[[:space:]]*}"
}

check_consistency() {
    local role="$1"
    local local_sha remote_sha
    require_main_branch
    require_clean_worktree
    local_sha="$(git -C "${project_root}" rev-parse HEAD)"
    remote_sha="$(remote_main_sha)"
    [[ "${local_sha}" == "${remote_sha}" ]] \
        || fail "HEAD ${local_sha} does not match origin/main ${remote_sha}"
    printf 'git_sync_ok role=%s head=%s\n' "${role}" "${local_sha}"
}

main() {
    [[ "$#" -eq 1 ]] || usage
    local command="$1"
    local role
    require_repository
    role="$(repository_role)"

    case "${command}" in
        check)
            check_consistency "${role}"
            ;;
        publish)
            [[ "${role}" == "server" ]] || fail "publish is restricted to saf.role=server"
            require_main_branch
            require_clean_worktree
            git -C "${project_root}" push origin HEAD:refs/heads/main
            check_consistency "${role}"
            ;;
        pull)
            [[ "${role}" == "mirror" ]] || fail "pull is restricted to saf.role=mirror"
            require_main_branch
            require_clean_worktree
            git -C "${project_root}" fetch --prune origin main
            git -C "${project_root}" merge-base --is-ancestor HEAD refs/remotes/origin/main \
                || fail "local main has commits not contained in origin/main"
            git -C "${project_root}" merge --ff-only refs/remotes/origin/main
            (cd -- "${project_root}" && uv sync --frozen --all-extras)
            check_consistency "${role}"
            ;;
        *) usage ;;
    esac
}

main "$@"
