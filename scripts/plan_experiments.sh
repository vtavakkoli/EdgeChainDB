#!/usr/bin/env sh
set -eu
CONFIG="${1:-full-matrix.yaml}"
REPETITIONS="${2:-}"
set -- docker compose --profile experiment run --rm experiment \
  python -m edgechaindb.experiments.runner \
  --config "/app/experiments/$CONFIG" \
  --result-dir /app/result/experiments \
  --dry-run
if [ -n "$REPETITIONS" ]; then set -- "$@" --repetitions "$REPETITIONS"; fi
"$@"
echo "Plan report: result/experiments/matrix-plan.html"
