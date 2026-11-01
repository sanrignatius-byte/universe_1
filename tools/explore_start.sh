#!/usr/bin/env bash
# Exploration: start probe receiver + generator on one docker network.
set -e
mkdir -p /root/explore/out /root/explore/runs
cp /mnt/h/exam-prep/exam-work/cosmos-observatory/tools/probe_receiver.py /root/explore/
docker network create cosmos-net >/dev/null 2>&1 || true
docker rm -f probe object-source >/dev/null 2>&1 || true
rm -f /root/explore/out/probe.jsonl
docker run -d --name probe --network cosmos-net -v /root/explore:/work -w /work python:3.11-slim python -I /work/probe_receiver.py /work/out/probe.jsonl >/dev/null
docker run -d --platform linux/arm64 --name object-source --network cosmos-net \
  --cpuset-cpus=0 --cpus=1 --memory=256m --memory-swap=256m \
  -p 127.0.0.1:9001:9001 -v /root/explore/runs:/runs talkingdata/flightgen:exam-v4-f1 >/dev/null
sleep 3
docker ps --format '{{.Names}} {{.Status}}'
