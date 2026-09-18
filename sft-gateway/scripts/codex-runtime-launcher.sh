#!/bin/sh
# Input: Harbor/Codex CLI arguments and the container environment, unchanged.
# Output: mounted Codex process stdout/stderr and exit status via exec.
# Requires the fixed /opt/codex runtime layout; it does not install a runtime,
# rewrite requests, or change Harbor's CODEX_HOME/authentication configuration.
exec /opt/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex "$@"
