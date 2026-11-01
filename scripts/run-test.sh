#!/usr/bin/env bash
# End-to-end run: start generator (if needed), drive one round against the
# observatory, fetch the receipt, check it belongs to this round, reconcile it.
#   bash scripts/run-test.sh TEST_ID [rate] [duration] [batch] [workers] [profile]
# Env:
#   GEN_TAR     path to flightgen-image.tar (only needed if the image is not loaded yet)
#   TARGET_URL  where the generator sends (default http://host.docker.internal:8000/ingest)
#   OBS_URL     where this script reads/reconciles (default http://127.0.0.1:8000)
#   GEN_URL     generator control API (default http://127.0.0.1:9001)
# Exit code is non-zero if the generator refuses to start, the receipt is from
# another round, or reconciliation finds an integrity problem.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
id=$1; rate=${2:-200}; dur=${3:-60}; batch=${4:-20}; workers=${5:-4}; profile=${6:-bursty}
OBS=${OBS_URL:-http://127.0.0.1:8000}
TARGET=${TARGET_URL:-http://host.docker.internal:8000/ingest}
export GEN_URL=${GEN_URL:-http://127.0.0.1:9001}
out_dir="$here/../results"
mkdir -p "$out_dir"

if ! docker image inspect talkingdata/flightgen:exam-v4-f1 >/dev/null 2>&1; then
  docker load -i "${GEN_TAR:?set GEN_TAR to flightgen-image.tar}"
fi
if ! docker ps --format '{{.Names}}' | grep -qx object-source; then
  docker rm -f object-source >/dev/null 2>&1 || true
  mkdir -p "$here/../generator-runs"
  # --add-host makes host.docker.internal work on Linux Docker Engine too (Docker Desktop has it built in)
  docker run -d --name object-source --platform linux/arm64 \
    --cpuset-cpus=0 --cpus=1 --memory=256m --memory-swap=256m \
    --add-host=host.docker.internal:host-gateway \
    -p 127.0.0.1:9001:9001 -v "$here/../generator-runs:/runs" \
    talkingdata/flightgen:exam-v4-f1 >/dev/null
  sleep 2
fi

[ "$rate" = 0 ] && export RAMP=0   # generator requires ramp 0 for best-effort mode
out=$(bash "$here/gen.sh" start "$id" "$TARGET" "$rate" "$dur" "$batch" "$workers" "$profile")
echo "$out" | head -c 300; echo
if ! echo "$out" | grep -q "\"test_id\":\"$id\"" || echo "$out" | grep -q '"error"'; then
  echo "generator refused to start: $out"; exit 1
fi
sleep 2
# sample resource usage while the round runs
( while true; do docker stats --no-stream --format '{{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}}' \
    cosmos-observatory object-source 2>/dev/null | sed "s/^/$(date +%T) /"; sleep 4; done ) \
  > "$out_dir/$id-docker-stats.txt" &
sampler=$!
trap 'kill $sampler 2>/dev/null || true' EXIT
while true; do
  state=$(curl -sS "$GEN_URL/status" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')
  [ "$state" = finished ] && break
  sleep 3
done
receipt="$out_dir/$id-receipt.json"
bash "$here/gen.sh" receipt "$receipt"
python3 - "$receipt" "$id" <<'EOF'
import json, sys
r = json.load(open(sys.argv[1]))
if r.get("config", {}).get("test_id") != sys.argv[2]:
    sys.exit(f"receipt belongs to {r.get('config', {}).get('test_id')!r}, not {sys.argv[2]!r}")
s = r["stats"]
keys = ["state", "finish_reason", "elapsed_seconds", "attempted_objects", "acknowledged_objects", "attempted_batches",
        "acknowledged_batches", "unacknowledged_batches", "retries", "timeouts", "http_4xx", "http_5xx",
        "network_errors", "average_latency_ms", "max_latency_ms"]
print("--- generator stats")
print({k: s.get(k) for k in keys})
print("objects/s acknowledged:", round(s["acknowledged_objects"] / max(s["elapsed_seconds"], 1e-9), 1))
EOF
echo "--- reconcile"
curl -sS -X POST "$OBS/api/reconcile" -H 'Content-Type: application/json' --data-binary @"$receipt" \
  > "$out_dir/$id-reconcile.json"
cat "$out_dir/$id-reconcile.json"; echo
echo "--- writer"
curl -sS "$OBS/api/overview?test=$id" | python3 -c 'import json,sys; print(json.load(sys.stdin)["writer"])'
echo "--- highest resource samples"
sort -t= -k2 -rn "$out_dir/$id-docker-stats.txt" | head -4
python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print("integrity_ok:", r["integrity_ok"], "exercise_complete:", r["exercise_complete"]); sys.exit(0 if r["integrity_ok"] else 3)' \
  "$out_dir/$id-reconcile.json"
