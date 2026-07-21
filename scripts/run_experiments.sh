#!/usr/bin/env sh
set -eu
CONFIG="${CONFIG:-full-matrix.yaml}"
SHARD_COUNT="${SHARD_COUNT:-1}"
SHARD_INDEX="${SHARD_INDEX:-0}"
MAX_RUNS="${MAX_RUNS:-}"
REPETITIONS="${REPETITIONS:-}"
docker compose build experiment
RESULT_DIR=/app/result/experiments
if [ "$SHARD_COUNT" -gt 1 ]; then RESULT_DIR=$(printf "/app/result/experiments/shard-%03d-of-%03d" "$SHARD_INDEX" "$SHARD_COUNT"); fi
set -- docker compose --profile experiment run --rm experiment \
  python -m edgechaindb.experiments.runner \
  --config "/app/experiments/$CONFIG" \
  --result-dir "$RESULT_DIR" \
  --resume --shard-count "$SHARD_COUNT" --shard-index "$SHARD_INDEX"
if [ -n "$MAX_RUNS" ]; then set -- "$@" --max-runs "$MAX_RUNS"; fi
if [ -n "$REPETITIONS" ]; then set -- "$@" --repetitions "$REPETITIONS"; fi
"$@"
echo "Matrix report written under ${RESULT_DIR#/app/}"
