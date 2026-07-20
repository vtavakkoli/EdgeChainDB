#!/usr/bin/env sh
set -u
repair="${1:-}"
failures=0

if [ "$repair" = "--repair" ]; then
  echo "==> rebuilding and recreating gateway"
  docker compose up -d --build --force-recreate gateway || exit $?
fi

echo "==> gateway state"
docker compose ps gateway || failures=$((failures + 1))

echo "==> published port"
port="$(docker compose port gateway 3030 2>&1)" || true
printf '%s\n' "$port"
echo "$port" | grep -q 3030 || failures=$((failures + 1))

echo "==> API health"
python - <<'PY' || failures=$((failures + 1))
import json, urllib.request
for url, key, expected in [
    ("http://127.0.0.1:8000/health", "status", "ok"),
    ("http://127.0.0.1:3030/monitor/health", "dashboard", "ready"),
]:
    with urllib.request.urlopen(url, timeout=8) as response:
        value = json.load(response)
    assert value[key] == expected, (url, value)
    print("PASS", url)
html = urllib.request.urlopen("http://127.0.0.1:3030/", timeout=8).read()
assert b"EdgeChainDB Cluster Monitor" in html
print("PASS dashboard HTML", len(html), "bytes")
info = json.load(urllib.request.urlopen(
    "http://127.0.0.1:3030/database/info?quick_check=true", timeout=60
))
assert info["quick_check"] == "ok", info
print("PASS database", info["engine"], info["pragmas"]["journal_mode"])
PY

echo "==> recent gateway logs"
docker compose logs --tail=120 gateway || true

if [ "$failures" -ne 0 ]; then
  echo "Dashboard diagnosis failed: $failures problem(s)."
  echo "Repair: ./scripts/diagnose_dashboard.sh --repair"
  exit 1
fi

echo "Dashboard diagnosis passed: http://127.0.0.1:3030"
