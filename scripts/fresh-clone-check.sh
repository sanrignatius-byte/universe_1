#!/usr/bin/env bash
# Reproduce from a fresh clone of the published repository: build, start, one
# generator round, reconcile, regression tests. Stops any running observatory first.
#   bash scripts/fresh-clone-check.sh [repo_url] [workdir]
set -euo pipefail
url=${1:-https://github.com/sanrignatius-byte/universe_1.git}
dir=${2:-/tmp/universe_1-fresh}
docker rm -f cosmos-observatory >/dev/null 2>&1 || true
rm -rf "$dir"
git clone -q "$url" "$dir"
cd "$dir"
echo "commit: $(git rev-parse HEAD)"
ls -l scripts/*.sh | awk '{print $1, $NF}'
docker compose -p universe1fresh up -d --build 2>&1 | tail -2
for _ in $(seq 1 60); do curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1 && break; sleep 1; done
curl -sS http://127.0.0.1:8000/api/health; echo
docker run --rm -i cosmos-observatory:latest python -I - /srv < tests/regression.py 2>&1 | grep -E 'PASS|FAIL|passed'
bash scripts/run-test.sh fresh-clone-001 300 30 20 4 bursty | tail -4
