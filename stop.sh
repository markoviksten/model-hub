#!/usr/bin/env bash
# Stop Model Hub. The container and its models are KEPT (never uses `rm`).
# Usage: ./stop.sh
set -euo pipefail

NAME="model-hub"
ENGINE="${ENGINE:-$(command -v docker >/dev/null 2>&1 && echo docker || { command -v podman >/dev/null 2>&1 && echo podman; })}"
[[ -n "$ENGINE" ]] || { echo "Neither docker nor podman found." >&2; exit 1; }

if ! "$ENGINE" container inspect "$NAME" >/dev/null 2>&1; then
  echo "Container $NAME not found."
  exit 0
fi
"$ENGINE" stop "$NAME" >/dev/null
echo "Stopped. Models are kept; start again with ./start.sh"
