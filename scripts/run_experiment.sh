#!/usr/bin/env sh
set -eu

docker compose up --build -d experiment
docker compose ps experiment
printf 'Report: %s/result/experiments/report.html
' "$PWD"
printf 'Progress: cat result/experiments/progress.json
'
if [ "${1:-}" = "--follow" ]; then
  docker compose logs -f experiment
fi
