#!/usr/bin/env bash
# Grow one world with the native replay bench, then time every UI endpoint on it.
#   bash scripts/scale-test.sh PROBE_JSONL TEST_ID [seconds] [processes]
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
probe=$1; id=$2; secs=${3:-180}; procs=${4:-16}
OBS=${OBS_URL:-http://127.0.0.1:8000}
docker run --rm --network host -v "$(dirname "$probe"):/probe:ro" -i python:3.11-slim \
  python -I - "/probe/$(basename "$probe")" "$OBS" "$id" "$procs" "$secs" 100 < "$here/../tools/bench.py"
docker stats --no-stream --format '{{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}}' cosmos-observatory
first=$(curl -sS "$OBS/api/objects?test=$id&limit=1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["items"][0]["id"])')
t() {  # median of 3 timings (first call may be cold)
  local name=$1 url=$2 a b c
  a=$(curl -sS -o /dev/null -w '%{time_total}' "$OBS$url"); b=$(curl -sS -o /dev/null -w '%{time_total}' "$OBS$url")
  c=$(curl -sS -o /dev/null -w '%{time_total}' "$OBS$url")
  awk -v n="$name" -v a="$a" -v b="$b" -v c="$c" \
    'BEGIN { w = (b < c ? b : c); printf "%-20s first %7.0f ms   best-of-2 %7.0f ms\n", n, a*1000, w*1000 }'
}
q() { python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$1"; }
echo "--- endpoint latency on $(curl -sS "$OBS/api/overview?test=$id" | python3 -c 'import json,sys; s=json.load(sys.stdin); print(s["stats"]["objects"], "objects,", round(s["db_bytes"]/2**30,2), "GiB")')"
t overview            "/api/overview?test=$id"
t tree                "/api/tree?test=$id"
t "objects newest"    "/api/objects?test=$id&limit=100"
t "objects by type"   "/api/objects?test=$id&type=10&limit=100"
t "objects page 2"    "/api/objects?test=$id&limit=100&before=1000000"
t "search common"     "/api/objects?test=$id&q=$(q 彗星)&limit=100"
t "search no match"   "/api/objects?test=$id&q=$(q 不存在的名字)&limit=100"
t "search exact id"   "/api/objects?test=$id&q=$first"
t "object detail"     "/api/object/$first"
t "sky map"           "/api/map?test=$id"
t fields              "/api/fields?test=$id"
t changes             "/api/changes?test=$id"
t rejects             "/api/rejects?test=$id"
