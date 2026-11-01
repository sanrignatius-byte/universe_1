#!/usr/bin/env bash
# Crash test: while the generator is sending, SIGKILL the observatory process
# (a real crash, not a graceful stop). Docker's restart policy brings it back.
# Afterwards reconcile: every batch we acknowledged must still be in the store.
#   scripts/crash-test.sh TEST_ID [rate] [duration] [kill_after_s] [kills]
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
id=$1; rate=${2:-2000}; dur=${3:-60}; kill_after=${4:-15}; kills=${5:-2}
(
  sleep "$kill_after"
  for i in $(seq 1 "$kills"); do
    pid=$(docker inspect -f '{{.State.Pid}}' cosmos-observatory)
    echo "[crash] $(date +%T) kill -9 pid $pid"
    kill -9 "$pid"
    for _ in $(seq 1 60); do
      curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1 && break
      sleep 0.5
    done
    echo "[crash] $(date +%T) service back: $(curl -sS http://127.0.0.1:8000/api/health)"
    sleep 10
  done
) &
killer=$!
RAMP=3 bash "$here/run-test.sh" "$id" "$rate" "$dur" 20 4 bursty
wait $killer || true
echo "restarts: $(docker inspect -f '{{.RestartCount}}' cosmos-observatory)"
