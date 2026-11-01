#!/usr/bin/env bash
# Generator control helper.
#   gen.sh start TEST_ID TARGET_URL [rate] [duration] [batch] [workers] [profile]
#   gen.sh status | stop | rate N | receipt OUTFILE
set -euo pipefail
G=${GEN_URL:-http://127.0.0.1:9001}
cmd=${1:-status}
case "$cmd" in
  start)
    id=$2; target=$3; rate=${4:-200}; dur=${5:-120}; batch=${6:-20}; workers=${7:-4}; profile=${8:-bursty}
    curl -sS "$G/start" -H 'Content-Type: application/json' -d "{
      \"workload\": \"objects\", \"target_url\": \"$target\", \"test_id\": \"$id\",
      \"seed\": ${SEED:-42}, \"batch_size\": $batch, \"workers\": $workers,
      \"objects_per_second\": $rate, \"rate_profile\": \"$profile\", \"ramp_seconds\": ${RAMP:-10},
      \"duration_seconds\": $dur, \"max_batches\": ${MAX_BATCHES:-20000}, \"retries\": 2,
      \"objects\": {\"scenario\": \"evolving\", \"tree_every_batches\": 10}}"
    echo ;;
  status) curl -sS "$G/status"; echo ;;
  stop) curl -sS -X POST "$G/stop"; echo ;;
  rate) curl -sS "$G/rate" -H 'Content-Type: application/json' -d "{\"objects_per_second\": $2}"; echo ;;
  receipt) curl -fsS "$G/receipt" -o "$2"; echo "saved $2" ;;
  *) echo "unknown $cmd"; exit 2 ;;
esac
