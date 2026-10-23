#!/usr/bin/env bash
# Start Model Hub: builds the image if missing, creates the container on first run, otherwise just starts it.
# Usage: ./start.sh
# Env:   MODEL_HUB_IMAGE (default model-hub:latest; use model-hub:airgap in the air-gapped site)
#        MODEL_HUB_LOADER_IMAGE (optional, helper pod image for the cluster copy)
#        MODEL_HUB_PORT (default 8767), ENGINE (docker|podman, auto-detected)
set -euo pipefail
cd "$(dirname "$0")"

NAME="model-hub"
PORT="${MODEL_HUB_PORT:-8767}"
IMAGE="${MODEL_HUB_IMAGE:-model-hub:latest}"
ENGINE="${ENGINE:-$(command -v docker >/dev/null 2>&1 && echo docker || { command -v podman >/dev/null 2>&1 && echo podman; })}"
[[ -n "$ENGINE" ]] || { echo "Neither docker nor podman found." >&2; exit 1; }

if ! "$ENGINE" info >/dev/null 2>&1; then
  echo "Cannot reach $ENGINE. Is it running? On Windows, run this shell and Rancher/Docker Desktop at the same privilege level." >&2
  exit 1
fi

if "$ENGINE" container inspect "$NAME" >/dev/null 2>&1; then
  if [[ "$("$ENGINE" container inspect -f '{{.State.Running}}' "$NAME")" == "true" ]]; then
    echo "Already running."
  else
    "$ENGINE" start "$NAME" >/dev/null
    echo "Started existing container (models kept)."
  fi
else
  if ! "$ENGINE" image inspect "$IMAGE" >/dev/null 2>&1; then
    if [[ -f Dockerfile ]]; then
      echo "Image $IMAGE not found, building..."
      "$ENGINE" build -t "$IMAGE" .
    else
      echo "Image $IMAGE not found. Load it first: $ENGINE load -i model-hub-with-models.tar.gz" >&2
      exit 1
    fi
  fi
  args=(-d --name "$NAME" --restart unless-stopped -p "127.0.0.1:${PORT}:8767")
  [[ -n "${MODEL_HUB_LOADER_IMAGE:-}" ]] && args+=(-e "MODEL_HUB_LOADER_IMAGE=${MODEL_HUB_LOADER_IMAGE}")
  "$ENGINE" run "${args[@]}" "$IMAGE" >/dev/null
  echo "Created and started a new container from $IMAGE."
fi
echo "Open: http://127.0.0.1:${PORT}"
