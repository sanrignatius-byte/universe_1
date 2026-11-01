#!/usr/bin/env bash
# A/B the ingest ceiling of several image/env variants with the native replay bench.
# Each run: fresh volume, same limits as compose (4 CPU, 4 GiB), 16 bench processes,
# batch 100. Variants alternate so that background load affects them equally.
#   bash scripts/ab-bench.sh PROBE_JSONL "label,image,ENV=VAL" ...
# Stops the compose container while running and starts it again at the end.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
probe=$1; shift
secs=${SECS:-30}; rounds=${ROUNDS:-2}
docker stop cosmos-observatory >/dev/null 2>&1 || true
for r in $(seq 1 "$rounds"); do
  for v in "$@"; do
    IFS=, read -r label image envs <<<"$v"
    eargs=(); for e in $envs; do eargs+=(-e "$e"); done
    docker rm -f abtest >/dev/null 2>&1; docker volume rm abvol >/dev/null 2>&1
    docker run -d --name abtest --init --cpus 4 --memory 4g -p 8000:8000 -v abvol:/data "${eargs[@]}" "$image" >/dev/null
    for _ in $(seq 1 60); do curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1 && break; sleep 0.5; done
    res=$(docker run --rm --network host -v "$(dirname "$probe"):/probe:ro" -i python:3.11-slim \
      python -I - "/probe/$(basename "$probe")" http://127.0.0.1:8000 "ab-$label-$r" 16 "$secs" 100 < "$here/../tools/bench.py")
    echo "round $r  $label  $res"
  done
done
docker rm -f abtest >/dev/null 2>&1; docker volume rm abvol >/dev/null 2>&1
docker start cosmos-observatory >/dev/null 2>&1 || true
