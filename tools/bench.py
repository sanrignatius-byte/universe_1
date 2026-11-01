"""Native load generator: replays real generator payloads (captured by the probe)
with fresh test_id / batch_id / sequence numbers, from several processes.

Measures the observatory's own ceiling, independent of the (QEMU-emulated) generator.
    python -I bench.py PROBE_JSONL URL TEST_ID PROCESSES SECONDS BATCH_SIZE
"""
import http.client
import json
import multiprocessing as mp
import sys
import time
from urllib.parse import urlparse


def load_templates(path):
    types, objs = [], []
    for line in open(path, encoding="utf-8"):
        r = json.loads(line)
        b = json.loads(r["body"])
        if b["kind"] == "types":
            types.append(b)
        elif b["batch_id"] < 20000:
            objs.extend(b["objects"])
    types.sort(key=lambda b: b["revision"])
    tpl = []
    for o in objs:
        o = dict(o, id="@ID@", sequence=-424242)
        tpl.append(json.dumps(o, ensure_ascii=False, separators=(",", ":")))
    return types, tpl, len(types)


def conn(url):
    u = urlparse(url)
    return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=10)


def post(c, body, headers):
    c.request("POST", "/ingest", body=body.encode(), headers=headers)
    r = c.getresponse()
    r.read()
    return r.status


def worker(idx, nproc, url, test_id, seconds, bsize, tpl, rev, out):
    c = conn(url)
    n_ok = n_obj = n_err = 0
    lat = []
    end = time.time() + seconds
    bid = idx
    while time.time() < end:
        parts = []
        for j in range(bsize):
            seq = bid * bsize + j
            s = tpl[seq % len(tpl)]
            # replace the sequence placeholder first and exactly: the id itself may contain "424242"
            parts.append(s.replace('"sequence":-424242', f'"sequence":{seq}').replace('"@ID@"', f'"{test_id}-{seq}"'))
        body = (f'{{"kind":"objects","test_id":"{test_id}","batch_id":{bid},"tree_revision":{rev},'
                f'"objects":[{",".join(parts)}]}}')
        h = {"Content-Type": "application/json", "X-Flightgen-Kind": "objects",
             "X-Flightgen-Test-Id": test_id, "X-Flightgen-Batch-Id": str(bid)}
        t0 = time.perf_counter()
        try:
            st = post(c, body, h)
        except Exception:
            n_err += 1
            c = conn(url)
            continue
        lat.append((time.perf_counter() - t0) * 1000)
        if 200 <= st < 300:
            n_ok += 1
            n_obj += bsize
        else:
            n_err += 1
        bid += nproc
    out.put((n_ok, n_obj, n_err, lat))


def main():
    path, url, test_id, nproc, seconds, bsize = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), float(sys.argv[5]), int(sys.argv[6])
    types, tpl, last_rev = load_templates(path)
    c = conn(url)
    for t in types:  # the tree must exist before objects reference it
        t = dict(t, test_id=test_id)
        st = post(c, json.dumps(t, ensure_ascii=False), {"Content-Type": "application/json", "X-Flightgen-Kind": "types",
                                                          "X-Flightgen-Test-Id": test_id, "X-Flightgen-Tree-Revision": str(t["revision"])})
        assert st == 200, st
    q = mp.Queue()
    ps = [mp.Process(target=worker, args=(i, nproc, url, test_id, seconds, bsize, tpl, last_rev, q)) for i in range(nproc)]
    t0 = time.time()
    for p in ps:
        p.start()
    res = [q.get() for _ in ps]
    for p in ps:
        p.join()
    dt = time.time() - t0
    ok = sum(r[0] for r in res); objs = sum(r[1] for r in res); err = sum(r[2] for r in res)
    lat = sorted(x for r in res for x in r[3])
    pct = lambda p: round(lat[min(len(lat) - 1, int(p * len(lat)))], 1) if lat else None
    print(json.dumps({"test_id": test_id, "processes": nproc, "batch_size": bsize, "seconds": round(dt, 1),
                      "acked_batches": ok, "acked_objects": objs, "errors": err,
                      "objects_per_s": round(objs / dt), "batches_per_s": round(ok / dt),
                      "latency_ms_p50": pct(0.5), "p99": pct(0.99), "max": pct(1.0)}))


if __name__ == "__main__":
    main()
