#!/usr/bin/env bash
# Crash test while the generator is sending, then reconcile: every acknowledged batch
# must still be in the store.
#   MODE=process   (default) SIGKILL the server process *inside* the container via
#                  `docker exec` (works the same on Docker Desktop, whose PIDs live in a VM).
#                  compose runs tini (`init: true`), so the container exits and Docker's
#                  restart policy brings it back: automatic recovery.
#   MODE=container `docker kill -s KILL` the whole container, then `docker start` it:
#                  manual recovery.
# The script verifies the container really restarted (StartedAt changed) and is healthy.
#   bash scripts/crash-test.sh TEST_ID [rate] [duration] [kill_after_s] [kills]
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
id=$1; rate=${2:-2000}; dur=${3:-60}; kill_after=${4:-15}; kills=${5:-1}
MODE=${MODE:-process}
OBS=${OBS_URL:-http://127.0.0.1:8000}
C=cosmos-observatory

crash() {
  if [ "$MODE" = container ]; then
    docker kill -s KILL "$C" >/dev/null && sleep 1 && docker start "$C" >/dev/null
  else
    docker exec "$C" python -c 'import os, signal
for p in os.listdir("/proc"):
    if p.isdigit() and int(p) != os.getpid():
        try:
            argv = open(f"/proc/{p}/cmdline", "rb").read().split(b"\0")
        except OSError:
            continue
        if len(argv) > 1 and argv[1].endswith(b"/uvicorn"):
            os.kill(int(p), signal.SIGKILL)
            print("SIGKILL uvicorn pid", p)'
  fi
}

(
  sleep "$kill_after"
  for i in $(seq 1 "$kills"); do
    started=$(docker inspect -f '{{.State.StartedAt}}' "$C")
    t0=$(date +%s.%N)
    echo "[crash] $(date +%T) mode=$MODE kill #$i"
    crash
    ok=0
    for _ in $(seq 1 120); do
      now=$(docker inspect -f '{{.State.StartedAt}}' "$C" 2>/dev/null || true)
      if [ -n "$now" ] && [ "$now" != "$started" ] && curl -fsS "$OBS/api/health" >/dev/null 2>&1; then ok=1; break; fi
      sleep 0.25
    done
    t1=$(date +%s.%N)
    if [ "$ok" = 1 ]; then
      echo "[crash] restarted and healthy after $(awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.1f", b-a}') s"
    else
      echo "[crash] FAILED: no restart observed within 30 s"; exit 1
    fi
    sleep 10
  done
) &
killer=$!
trap 'kill $killer 2>/dev/null || true' EXIT

rc=0
RAMP=3 bash "$here/run-test.sh" "$id" "$rate" "$dur" 20 4 bursty || rc=$?
if [ "$rc" -ne 0 ]; then
  kill "$killer" 2>/dev/null || true   # do not crash the service after a failed round
fi
wait "$killer" || { [ "$rc" -ne 0 ] || { echo "crash injection failed"; rc=1; }; }
echo "restart count: $(docker inspect -f '{{.RestartCount}}' "$C")"
exit "$rc"
