#!/usr/bin/env bash
# Smoke-test every read endpoint of a running observatory.
OBS=${OBS_URL:-http://127.0.0.1:8000}
for p in /api/health /api/tests /api/overview /api/tree "/api/objects?limit=2" "/api/changes?limit=3" \
         "/api/fields?limit=3" "/api/map?limit=3" "/api/rejects?limit=2"; do
  echo "== $p"
  curl -sS "$OBS$p" | head -c ${SMOKE_CHARS:-600}
  echo
done
for p in / /static/app.js /static/style.css /static/vendor/echarts.min.js; do
  curl -sS -o /dev/null -w "$p %{http_code} %{size_download}\n" "$OBS$p"
done
