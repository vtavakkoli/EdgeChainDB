#!/usr/bin/env sh
set -eu
CONFIG="${CONFIG:-full-matrix.yaml}"
INPUT="${INPUT:-/result/experiments/shard-*}"
OUTPUT="${OUTPUT:-/result/experiments/combined}"
docker compose --profile experiment run --rm experiment \
  python -m edgechaindb.experiments.merge \
  --config "/app/experiments/$CONFIG" \
  --input "$INPUT" --output "$OUTPUT"
echo "Combined report: result/experiments/combined/report.html"
