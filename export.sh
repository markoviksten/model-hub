#!/usr/bin/env bash
# Export the Model Hub container WITH its models to dist/ for transfer to the air-gapped site.
# Usage: ./export.sh [--no-compress] [--keep-online] [-c <container>] [-t <image:tag>]
# Output: dist/model-hub-with-models.tar.gz + .sha256 (copy both to the USB stick).
set -euo pipefail
cd "$(dirname "$0")"

CONTAINER="model-hub"
TAG="model-hub:airgap"
COMPRESS=1
OFFLINE=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-compress) COMPRESS=0 ;;
    --keep-online) OFFLINE=0 ;;
    -c) CONTAINER="${2:?missing value for -c}"; shift ;;
    -t) TAG="${2:?missing value for -t}"; shift ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
[[ "$CONTAINER" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || { echo "Invalid container name." >&2; exit 2; }
[[ "$TAG" =~ ^[A-Za-z0-9][A-Za-z0-9_.:/-]*$ ]] || { echo "Invalid image tag." >&2; exit 2; }

ENGINE="${ENGINE:-$(command -v docker >/dev/null 2>&1 && echo docker || { command -v podman >/dev/null 2>&1 && echo podman; })}"
[[ -n "$ENGINE" ]] || { echo "Neither docker nor podman found." >&2; exit 1; }
"$ENGINE" container inspect "$CONTAINER" >/dev/null 2>&1 || { echo "Container '$CONTAINER' not found (or $ENGINE not reachable)." >&2; exit 1; }

if "$ENGINE" container inspect "$CONTAINER" --format '{{range .Mounts}}{{.Destination}} {{end}}' | grep -q '/models'; then
  echo "Container '$CONTAINER' has /models on a volume/bind mount; those files are NOT included in a commit." >&2
  exit 1
fi

echo "Committing $CONTAINER -> $TAG (includes /models)..."
commit_args=(commit)
[[ "$OFFLINE" == 1 ]] && commit_args+=(--change "ENV MODEL_HUB_OFFLINE=1")
"$ENGINE" "${commit_args[@]}" "$CONTAINER" "$TAG" >/dev/null

mkdir -p dist
OUT="dist/model-hub-with-models.tar"
rm -f "$OUT" "$OUT.gz" "$OUT.gz.sha256" "$OUT.sha256"
echo "Saving image (large models take a few minutes)..."
if [[ "$COMPRESS" == 1 ]]; then
  FINAL="$OUT.gz"
  "$ENGINE" save "$TAG" | gzip > "$FINAL"
else
  FINAL="$OUT"
  "$ENGINE" save -o "$FINAL" "$TAG"
fi

( cd dist && sha256sum "$(basename "$FINAL")" > "$(basename "$FINAL").sha256" )
echo "Archive: $FINAL ($(du -h "$FINAL" | cut -f1))"
cat "$FINAL.sha256"
echo "Copy BOTH files to the USB stick: $FINAL and $FINAL.sha256"
echo "On the target:  sha256sum -c $(basename "$FINAL").sha256 && $ENGINE load -i $(basename "$FINAL")"
