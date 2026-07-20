#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."

# Keep named volumes so restart recovery and storage-growth tests operate on the
# same durable system. Use `docker compose down -v` only for an intentional reset.
docker compose --profile test up -d --build --force-recreate test

container_id=$(docker compose --profile test ps --all --quiet test | head -n 1)
if [ -z "$container_id" ]; then
  echo "No container found for test" >&2
  exit 1
fi

exit_code=$(docker wait "$container_id")
mkdir -p result
docker logs "$container_id" > result/test-container.log 2>&1 || true

if [ "$exit_code" != "0" ]; then
  echo "Benchmark failed with exit code $exit_code." >&2
  echo "Open result/report.html and result/benchmarks/report.html." >&2
  exit "$exit_code"
fi

echo "Dashboard:      http://127.0.0.1:3030"
echo "System report:  result/report.html"
echo "Research report: result/benchmarks/report.html"
