#!/usr/bin/env bash
# The measurement suite behind README §7 (fresh volume, final code).
#   bash scripts/full-suite.sh /path/to/probe.jsonl
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
cd "$here/.."
probe=${1:-}
docker compose down -v >/dev/null 2>&1
docker compose up -d --build >/dev/null 2>&1
for _ in $(seq 1 60); do curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1 && break; sleep 1; done
run() { echo; echo "################ $*"; "$@"; echo "exit=$?"; }
run bash scripts/run-test.sh final-example 200 120 20 4 bursty          # the parameters from the task sheet
run bash scripts/run-test.sh final-max-b20 0 60 20 4 steady              # best effort, default shape
MAX_BATCHES=100000 run bash scripts/run-test.sh final-max-b100 0 60 100 16 steady
run bash scripts/crash-test.sh final-crash 2000 40 12 1
if [ -n "$probe" ]; then
  echo; echo "################ native bench (observatory ceiling, generator not involved)"
  docker run --rm --network host -v "$(dirname "$probe"):/probe:ro" -i python:3.11-slim \
    python -I - "/probe/$(basename "$probe")" http://127.0.0.1:8000 final-bench 16 30 100 < tools/bench.py
fi
echo; echo "################ storage after suite"
curl -sS http://127.0.0.1:8000/api/overview | python3 -c 'import json,sys; o=json.load(sys.stdin); print("db_bytes", o["db_bytes"], "disk", o["disk"])'
docker stats --no-stream cosmos-observatory
