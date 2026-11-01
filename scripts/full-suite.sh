#!/usr/bin/env bash
# The measurement suite behind README §7. Recreates the data volume of this compose
# project (it deletes earlier measurement data!), then runs every round with fresh test ids.
#   bash scripts/full-suite.sh [/path/to/probe.jsonl]      (probe enables the native bench)
# Exit code is non-zero if any round fails.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
cd "$here/.."
probe=${1:-}
tag=${RUN_TAG:-$(date +%H%M%S)}   # the generator refuses to reuse a test_id
fail=0
docker compose down -v >/dev/null 2>&1
docker compose up -d --build >/dev/null 2>&1 || { echo "build/start failed"; exit 1; }
healthy=0
for _ in $(seq 1 60); do curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1 && { healthy=1; break; }; sleep 1; done
[ "$healthy" = 1 ] || { echo "service did not become healthy"; exit 1; }

run() {
  echo; echo "################ $*"
  "$@"
  local rc=$?
  echo "exit=$rc"
  [ "$rc" -eq 0 ] || fail=1
}
run bash scripts/run-test.sh "example-$tag" 200 120 20 4 bursty          # parameters from the task sheet
run bash scripts/run-test.sh "max-b20-$tag" 0 60 20 4 steady              # best effort, default shape
MAX_BATCHES=100000 run bash scripts/run-test.sh "max-b100-$tag" 0 60 100 16 steady
run bash scripts/crash-test.sh "crash-$tag" 2000 40 12 1
if [ -n "$probe" ]; then
  run docker run --rm --network host -v "$(dirname "$probe"):/probe:ro" -i python:3.11-slim \
    python -I - "/probe/$(basename "$probe")" http://127.0.0.1:8000 "bench-$tag" 16 30 100 < tools/bench.py
fi
echo; echo "################ storage after suite"
curl -sS http://127.0.0.1:8000/api/overview | python3 -c 'import json,sys; o=json.load(sys.stdin); print("db_bytes", o["db_bytes"], "disk", o["disk"])'
docker stats --no-stream cosmos-observatory
echo "suite result: $([ "$fail" = 0 ] && echo PASS || echo FAIL)"
exit "$fail"
