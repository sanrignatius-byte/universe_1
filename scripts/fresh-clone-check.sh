#!/usr/bin/env bash
# Reproduce from a fresh clone of the published repository: build, start, regression
# tests, one generator round, reconcile. Stops the running observatory container first.
#   bash scripts/fresh-clone-check.sh [repo_url] [new_dir]
# new_dir must not exist (the script never deletes anything); default: a new mktemp dir.
set -euo pipefail
url=${1:-https://github.com/sanrignatius-byte/universe_1.git}
dir=${2:-}
if [ -z "$dir" ]; then
  dir="$(mktemp -d "${TMPDIR:-/tmp}/universe_1-fresh.XXXXXX")/repo"
elif [ -e "$dir" ]; then
  echo "refusing to use existing path: $dir" >&2; exit 2
fi
docker rm -f cosmos-observatory >/dev/null 2>&1 || true
git clone -q "$url" "$dir"
cd "$dir"
echo "commit: $(git rev-parse HEAD)   dir: $dir"
ls -l scripts/*.sh | awk '{print $1, $NF}'
docker compose -p universe1fresh up -d --build 2>&1 | tail -2
for _ in $(seq 1 60); do curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1 && break; sleep 1; done
curl -fsS http://127.0.0.1:8000/api/health; echo
docker run --rm -i cosmos-observatory:latest python -I - /srv < tests/regression.py 2>&1 | grep -E 'PASS|FAIL|passed'
bash scripts/run-test.sh "fresh-clone-$(date +%H%M%S)" 300 30 20 4 bursty | tail -4
