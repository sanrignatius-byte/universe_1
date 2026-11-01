#!/usr/bin/env bash
# Same best-effort round with synchronous=NORMAL, then restore the default (FULL).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
cd "$here/.."
SQLITE_SYNC=NORMAL docker compose up -d --build >/dev/null 2>&1
sleep 5
curl -sS http://127.0.0.1:8000/api/overview | python3 -c 'import json,sys; print("sync mode:", json.load(sys.stdin)["writer"]["sync"])' || true
MAX_BATCHES=100000 bash "$here/run-test.sh" "${1:-perf-003-normal}" 0 60 100 16 steady 2>&1 | tail -12
docker compose up -d >/dev/null 2>&1   # back to FULL
sleep 5
curl -sS http://127.0.0.1:8000/api/overview | python3 -c 'import json,sys; print("restored sync mode:", json.load(sys.stdin)["writer"]["sync"])'
