#!/usr/bin/env bash
# End-to-end run: start generator (if needed), drive one round against the
# observatory, fetch the receipt and reconcile it with the store.
#   scripts/run-test.sh TEST_ID [rate] [duration] [batch] [workers] [profile]
# Env: GEN_TAR (path to flightgen-image.tar), OBS_URL (default http://127.0.0.1:8000)
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
id=$1; rate=${2:-200}; dur=${3:-60}; batch=${4:-20}; workers=${5:-4}; profile=${6:-bursty}
OBS=${OBS_URL:-http://127.0.0.1:8000}
mkdir -p "$here/../results"

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

bash "$here/gen.sh" start "$id" http://host.docker.internal:8000/ingest "$rate" "$dur" "$batch" "$workers" "$profile"
while true; do
  s=$(curl -sS http://127.0.0.1:9001/status)
  state=$(echo "$s" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')
  [ "$state" = finished ] && break
  sleep 3
done
receipt="$here/../results/$id-receipt.json"
bash "$here/gen.sh" receipt "$receipt"
echo "--- generator stats"
python3 - "$receipt" <<'EOF'
import json,sys
s=json.load(open(sys.argv[1]))["stats"]
keys=["elapsed_seconds","attempted_objects","acknowledged_objects","attempted_batches","acknowledged_batches",
      "unacknowledged_batches","retries","timeouts","http_4xx","http_5xx","network_errors","average_latency_ms","max_latency_ms"]
print({k:s.get(k) for k in keys})
print("objects/s acknowledged:", round(s["acknowledged_objects"]/max(s["elapsed_seconds"],1e-9),1))
EOF
echo "--- reconcile"
curl -sS -X POST "$OBS/api/reconcile" -H 'Content-Type: application/json' --data-binary @"$receipt" \
  | tee "$here/../results/$id-reconcile.json"
echo
