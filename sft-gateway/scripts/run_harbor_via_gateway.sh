#!/usr/bin/env bash
set -euo pipefail
umask 077

# Input: TASK_PATH, optional Harbor CLI arguments, and the trusted SFT_ENV_FILE.
# Starts a gateway and Harbor's native Codex agent, then exports the captured run.
# Output: a SQLite capture, gateway log, Harbor job/config, and SFT export folder.
# The gateway reads the upstream billing key; the container receives a separate
# generated/configured gateway key through native OPENAI_API_KEY authentication.
# Exit status prefers a Harbor failure, otherwise the export command's status.
# Requires the local Python/Harbor/Docker tools, a prepared task, and mounted
# Codex runtime. Task completion and Harbor verification are not implied by SFT
# export success; inspect Harbor's result and the gateway export report separately.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${SFT_ENV_FILE:-/data/hy/wenwu/science-agent-factory/.env}"
TASK_PATH="${1:-}"
if [[ -z "$TASK_PATH" || "$TASK_PATH" == "--help" || "$TASK_PATH" == "-h" ]]; then
  echo "usage: $0 TASK_PATH [-- extra harbor run options]" >&2
  exit 2
fi
shift
if [[ "${1:-}" == "--" ]]; then shift; fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing env file: $ENV_FILE" >&2
  exit 2
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
: "${SAF_CODEX_BASE_URL:?SAF_CODEX_BASE_URL is missing}"
: "${SAF_CODEX_API_KEY:?SAF_CODEX_API_KEY is missing}"

RUN_ID="${SFT_RUN_ID:-$(basename "$TASK_PATH")-$(date +%Y%m%d-%H%M%S)}"
GATEWAY_API_KEY="${SFT_GATEWAY_API_KEY:-$(python - <<'PY'
import secrets
print("sft-gateway-" + secrets.token_urlsafe(24))
PY
)}"
ADMIN_API_KEY="${SFT_ADMIN_API_KEY:-$(python -c 'import secrets; print(secrets.token_urlsafe(32))')}"
INSTANCE_ID="$(python -c 'import secrets; print(secrets.token_hex(12))')"
GATEWAY_PORT="${SFT_GATEWAY_PORT:-18110}"
GATEWAY_HOST="${SFT_GATEWAY_HOST:-$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ {for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')}"
GATEWAY_HOST="${GATEWAY_HOST:-$(hostname -I | awk '{print $1}')}"
DB_PATH="${SFT_DB:-$ROOT_DIR/data/harbor/$RUN_ID.sqlite}"
JOBS_DIR="${HARBOR_JOBS_DIR:-$ROOT_DIR/artifacts/harbor-jobs}"
MODEL="${SFT_MODEL:-gpt-6-astra}"
CODEX_CONFIG="${SFT_CODEX_CONFIG:-$ROOT_DIR/configs/codex-gpt6-astra-high.toml}"
CODEX_RUNTIME="${SFT_CODEX_RUNTIME:-/data/hy/wenwu/science-agent-factory/var/tb-science-official-v0.1.0-astra/codex-runtime-0.1534}"
CODEX_WRAPPER="${SFT_CODEX_WRAPPER:-$ROOT_DIR/scripts/codex-runtime-launcher.sh}"
PREWARMED_TASK="${SFT_PREWARMED_TASK:-$TASK_PATH}"
PREWARMED_IMAGE="${SFT_PREWARMED_IMAGE:-$(python - "$PREWARMED_TASK/task.toml" <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as handle:
    print(tomllib.load(handle).get("environment", {}).get("docker_image", ""))
PY
 )}"
HARBOR_CONFIG="${SFT_HARBOR_CONFIG:-$ROOT_DIR/data/harbor-$RUN_ID.config.json}"
# The generated config contains the gateway credential; keep it out of commits
# and shared artifacts. The current script does not remove it on exit.
mkdir -p "$(dirname "$DB_PATH")" "$JOBS_DIR"

if [[ ! -d "$CODEX_RUNTIME" ]]; then
  echo "missing Codex runtime directory: $CODEX_RUNTIME" >&2
  exit 2
fi
if [[ ! -x "$CODEX_WRAPPER" ]]; then
  echo "missing executable Codex wrapper: $CODEX_WRAPPER" >&2
  exit 2
fi
if [[ ! -d "$PREWARMED_TASK" ]]; then
  echo "missing Harbor task path: $PREWARMED_TASK" >&2
  exit 2
fi
if [[ -z "$PREWARMED_IMAGE" ]]; then
  echo "task.toml must select its prewarmed environment.docker_image; set SFT_PREWARMED_TASK to the prepared task copy" >&2
  exit 2
fi

# Validate the actual Harbor task selection, not an unrelated preflight image.
actual_image="$(python - "$PREWARMED_TASK/task.toml" <<'PYIMAGE'
import sys, tomllib
with open(sys.argv[1], "rb") as handle:
    print(tomllib.load(handle).get("environment", {}).get("docker_image", ""))
PYIMAGE
)"
if [[ "$actual_image" != "$PREWARMED_IMAGE" ]]; then
  echo "SFT_PREWARMED_IMAGE must match PREWARMED_TASK/task.toml; prepare a task copy with the selected image" >&2
  exit 2
fi

if [[ "$GATEWAY_HOST" == "127.0.0.1" || "$GATEWAY_HOST" == "localhost" ]]; then
  echo "gateway host must be reachable from Docker; set SFT_GATEWAY_HOST to the host IP" >&2
  exit 2
fi

SFT_INSTANCE_ID="$INSTANCE_ID" \
SFT_UPSTREAM_URL="$SAF_CODEX_BASE_URL" \
SFT_UPSTREAM_API_KEY="$SAF_CODEX_API_KEY" \
SFT_GATEWAY_API_KEY="$GATEWAY_API_KEY" \
SFT_ADMIN_API_KEY="$ADMIN_API_KEY" \
SFT_STRIP_CODEX_INTERNAL_METADATA="${SFT_STRIP_CODEX_INTERNAL_METADATA:-1}" \
SFT_DB="$DB_PATH" \
PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
python -m uvicorn sft_gateway.app:app --host 0.0.0.0 --port "$GATEWAY_PORT" --log-level warning \
  >"$ROOT_DIR/data/harbor-$RUN_ID.gateway.log" 2>&1 &
GATEWAY_PID=$!
# Input: this script's gateway PID. Stop/wait best effort, with no return payload.
# This trap does not export trajectories or manage Harbor's container lifecycle;
# the normal export is a separate step after `harbor run` returns below.
cleanup() { kill "$GATEWAY_PID" 2>/dev/null || true; wait "$GATEWAY_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

# Input: health response JSON; accept only this launch's random instance ID.
ready() {
  curl -fsS "http://127.0.0.1:$GATEWAY_PORT/health" | python -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("instance_id")==sys.argv[1] else 1)' "$INSTANCE_ID"
}
for _ in $(seq 1 60); do
  if ! kill -0 "$GATEWAY_PID" 2>/dev/null; then
    echo "gateway failed to start; inspect the gateway log" >&2
    exit 2
  fi
  if ready >/dev/null 2>&1; then break; fi
  sleep 1
done
kill -0 "$GATEWAY_PID"
ready >/dev/null

# Use the Docker daemon that contains the preheated task and verifier images.
# Also remove the real upstream credentials from this shell before invoking
# Harbor; only the separate gateway credential below is passed to the container.
export DOCKER_HOST="${SFT_DOCKER_HOST:-${SAF_DOCKER_HOST:-}}"
if [[ -z "$DOCKER_HOST" ]]; then
  echo "SFT_DOCKER_HOST or SAF_DOCKER_HOST is required for the prewarmed Docker daemon" >&2
  exit 2
fi
unset SAF_DOCKER_HOST SAF_ROOTLESS_DOCKER_ROOT
unset SAF_CODEX_API_KEY SAF_CODEX_BASE_URL SAF_CLAUDE_API_KEY
unset SFT_UPSTREAM_API_KEY SFT_UPSTREAM_URL OPENAI_API_KEY OPENAI_BASE_URL SFT_ADMIN_API_KEY ADMIN_API_KEY

export RUN_ID JOBS_DIR MODEL CODEX_CONFIG CODEX_RUNTIME CODEX_WRAPPER PREWARMED_TASK PREWARMED_IMAGE GATEWAY_API_KEY
export GATEWAY_URL="http://$GATEWAY_HOST:$GATEWAY_PORT/runs/$RUN_ID/v1"

python - "$HARBOR_CONFIG" <<'PY'
import json
import os
import sys

config_path = sys.argv[1]
payload = {
    "job_name": os.environ["RUN_ID"],
    "jobs_dir": os.environ["JOBS_DIR"],
    "n_attempts": 1,
    "n_concurrent_trials": 1,
    "agent_setup_timeout_multiplier": 4.0,
    "tasks": [{"path": os.environ["PREWARMED_TASK"]}],
    "agents": [{
        "name": "codex",
        "model_name": os.environ["MODEL"],
        "kwargs": {
            "version": "0.153.4",
            "reasoning_effort": "high",
            "config": os.environ["CODEX_CONFIG"],
        },
        "env": {
            "OPENAI_BASE_URL": os.environ["GATEWAY_URL"],
            "OPENAI_API_KEY": os.environ["GATEWAY_API_KEY"],
        },
    }],
    "environment": {
        "type": "docker",
        "mounts": [
            {
                "type": "bind",
                "source": os.environ["CODEX_RUNTIME"],
                "target": "/opt/codex",
                "read_only": True,
                "bind": {"create_host_path": False},
            },
            {
                "type": "bind",
                "source": os.environ["CODEX_WRAPPER"],
                "target": "/usr/local/bin/codex",
                "read_only": True,
                "bind": {"create_host_path": False},
            },
        ],
    },
}
with open(config_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY

# Validate the mounted runtime before spending model requests.  The command
# uses the selected preflight image and the same runtime/wrapper mounts. Harbor
# itself selects the same image from PREWARMED_TASK/task.toml; mismatched
# SFT_PREWARMED_IMAGE overrides are rejected above.
runtime_version="$(docker run --rm \
  -v "$CODEX_RUNTIME:/opt/codex:ro" \
  -v "$CODEX_WRAPPER:/usr/local/bin/codex:ro" \
  --entrypoint /usr/local/bin/codex \
  "$PREWARMED_IMAGE" --version)"
echo "$runtime_version"
if [[ "$runtime_version" != "codex-cli 0.153.4" ]]; then
  echo "expected codex-cli 0.153.4; refusing to launch the task" >&2
  exit 2
fi

set +e
harbor run --config "$HARBOR_CONFIG" --yes "$@"
harbor_rc=$?
set -e

set +e
# Export reads the capture as it exists when Harbor returns. The command does
# writes rejected leaves only to report-only incomplete files; the normal SFT
# set still requires eligibility and tool closure.
PYTHONPATH="$ROOT_DIR" python -m sft_gateway.cli export "$RUN_ID" \
  --db "$DB_PATH" --output "$ROOT_DIR/artifacts/harbor-$RUN_ID" \
  --task "$TASK_PATH" --include-incomplete
export_rc=$?
set -e
if [[ "$harbor_rc" -ne 0 ]]; then
  echo "Harbor exited with status $harbor_rc; raw/incomplete capture was exported." >&2
  exit "$harbor_rc"
fi
exit "$export_rc"
