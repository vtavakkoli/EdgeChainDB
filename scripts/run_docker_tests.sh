#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."

docker compose --profile test down -v --remove-orphans
docker compose build
docker compose --profile test up -d test

# --all is required because the benchmark is an intentional one-shot container.
container_id=$(docker compose --profile test ps --all --quiet test | head -n 1)
if [ -z "$container_id" ]; then
  echo "No container found for test" >&2
  exit 1
fi

exit_code=$(docker wait "$container_id")
docker logs "$container_id" > result/test-container.log 2>&1 || true

if [ "$exit_code" != "0" ]; then
  echo "Benchmark failed with exit code $exit_code. Open result/report.html." >&2
  exit "$exit_code"
fi

echo "Dashboard: http://localhost:8000/dashboard"
echo "Report:    result/report.html"
