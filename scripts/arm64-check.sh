#!/usr/bin/env bash
# Build the image for linux/arm64 (the acceptance platform) and check that the
# native wheels import. On x86 hosts this needs QEMU binfmt (tonistiigi/binfmt).
set -euo pipefail
cd "$(dirname "$0")/.."
docker buildx build --platform linux/arm64 -t cosmos-observatory:arm64-check --load . 2>&1 | tail -4
docker run --rm --platform linux/arm64 cosmos-observatory:arm64-check \
  python -c 'import platform, orjson, uvloop, httptools, fastapi, sqlite3; print(platform.machine(), "sqlite", sqlite3.sqlite_version, "imports ok")'
