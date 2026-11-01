"""Where does ingest CPU go? Times parse / validate / writer-apply / commit on real payloads.
    python -I profile_store.py REPO_DIR PROBE_JSONL [n_batches] [sync]
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, sys.argv[1])
os.environ["SQLITE_SYNC"] = sys.argv[4] if len(sys.argv) > 4 else "FULL"
import orjson  # noqa: E402
from app import validate  # noqa: E402
from app.store import Job, Store  # noqa: E402

recs = [json.loads(l) for l in open(sys.argv[2], encoding="utf-8")]
types = sorted((json.loads(r["body"]) for r in recs if json.loads(r["body"])["kind"] == "types"), key=lambda b: b["revision"])
objs = [o for r in recs for o in json.loads(r["body"]).get("objects", []) if json.loads(r["body"]).get("batch_id", 99999) < 20000]
N = int(sys.argv[3]) if len(sys.argv) > 3 else 400
B = 100
bodies = []
for i in range(N):
    batch = []
    for j in range(B):
        o = dict(objs[(i * B + j) % len(objs)])
        o["sequence"] = i * B + j
        o["id"] = f"prof-{i * B + j}"
        batch.append(o)
    bodies.append(orjson.dumps({"kind": "objects", "test_id": "prof", "batch_id": i, "tree_revision": types[-1]["revision"], "objects": batch}))

d = tempfile.mkdtemp()
st = Store(os.path.join(d, "p.db"))
st.close()  # stop the thread; drive the writer synchronously
st.conn = Store._open(os.path.join(d, "p.db"))
for t in types:
    t["test_id"] = "prof"
    st._reset_acc(); st.conn.execute("BEGIN IMMEDIATE"); st._apply(Job("types", t, 100)); st._flush_acc(); st.conn.execute("COMMIT")

T = {"parse": 0.0, "validate": 0.0, "apply": 0.0, "commit": 0.0}
G = int(os.environ.get("GROUP", "8"))
for g in range(0, N, G):
    jobs = []
    for b in bodies[g:g + G]:
        t0 = time.perf_counter(); m = orjson.loads(b); T["parse"] += time.perf_counter() - t0
        t0 = time.perf_counter(); assert validate.check_objects(m, None, None) is None; T["validate"] += time.perf_counter() - t0
        jobs.append(Job("objects", m, len(b), B))
    t0 = time.perf_counter()
    st._reset_acc(); st.conn.execute("BEGIN IMMEDIATE")
    for j in jobs:
        assert st._apply(j)[0] == 200
    st._flush_acc()
    T["apply"] += time.perf_counter() - t0
    t0 = time.perf_counter(); st.conn.execute("COMMIT"); T["commit"] += time.perf_counter() - t0
tot = sum(T.values())
print(json.dumps({"batches": N, "batch_size": B, "group": G, "sync": os.environ["SQLITE_SYNC"],
                  "ms_per_batch": {k: round(v / N * 1000, 3) for k, v in T.items()},
                  "objects_per_s_single_thread": round(N * B / tot)}))
