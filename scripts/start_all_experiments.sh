#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
mkdir -p result/experiments
docker compose up --build -d experiment
echo "Logs:     docker compose logs -f experiment"
echo "Progress: result/experiments/progress.json"
echo "Report:   result/experiments/report.html"
