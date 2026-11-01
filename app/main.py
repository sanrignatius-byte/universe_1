import asyncio
import base64
import logging
import os
import queue
import time
from contextlib import asynccontextmanager

import orjson
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, ORJSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import validate
from .store import Job, Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
DB_PATH = os.environ.get("DB_PATH", "/data/cosmos.db")
STATIC = os.path.join(os.path.dirname(__file__), "static")
MAX_BODY = 64 * 1024 * 1024
ENQUEUE_WAIT_S = float(os.environ.get("ENQUEUE_WAIT_S", "2.0"))

store: Store = None
started = time.time()


@asynccontextmanager
async def lifespan(app):
    global store
    store = Store(DB_PATH)
    yield
    store.close()  # drains the queue: every acknowledged message is committed


app = FastAPI(title="Cosmos Observatory", default_response_class=ORJSONResponse, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _set(fut, v):
    if not fut.done():
        fut.set_result(v)


async def _submit(job):
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    job.done = lambda code, detail: loop.call_soon_threadsafe(_set, fut, (code, detail))
    deadline = loop.time() + ENQUEUE_WAIT_S
    while True:
        try:
            store.submit_nowait(job)
            break
        except queue.Full:  # backpressure: hold the request briefly instead of failing fast
            if loop.time() > deadline:
                store.metrics["http_503"] += 1
                return 503, "busy"
            await asyncio.sleep(0.005)
    return await fut


def _resp(code, detail):
    return Response(content=orjson.dumps({"status": detail}), status_code=code, media_type="application/json")


@app.post("/ingest")
async def ingest(request: Request):
    body = await request.body()
    h = request.headers
    if len(body) > MAX_BODY:
        return _resp(413, "too large")
    try:
        m = orjson.loads(body)
    except orjson.JSONDecodeError as e:
        m, err, code = {"kind": h.get("x-flightgen-kind"), "test_id": h.get("x-flightgen-test-id")}, f"invalid JSON: {e}", 400
    else:
        code = 422
        if type(m) is not dict:
            m, err = {}, "body is not a JSON object"
        else:
            kind = m.get("kind")
            if kind == "objects":
                err = validate.check_objects(m, h.get("x-flightgen-test-id"), h.get("x-flightgen-batch-id"))
            elif kind == "types":
                err = validate.check_types(m, h.get("x-flightgen-test-id"), h.get("x-flightgen-tree-revision"))
            else:
                err = f"unknown kind {kind!r}"
    if err:
        j = Job("reject", m, len(body))
        j.code, j.reason, j.sample = code, err, body[:4000].decode("utf-8", "replace")
        code, detail = await _submit(j)
        if code == 503:
            return _resp(503, "busy")
        return _resp(j.code, err)
    cost = len(m["objects"]) if m["kind"] == "objects" else 1
    code, detail = await _submit(Job(m["kind"], m, len(body), cost))
    return _resp(code, detail)


# ---------------------------------------------------------------- read API
def _tk(test):
    if test:
        k = store.tests.get(test)
        if k is None:
            raise HTTPException(404, f"unknown test {test}")
        return k
    r = store.reader().execute("SELECT test_key FROM test_stats ORDER BY last_ms DESC LIMIT 1").fetchone()
    if not r:
        raise HTTPException(404, "no data yet")
    return r[0]


def _subtree(tk, type_id):
    tree = store.trees.get(tk, {})
    kids = {}
    for tid, (pid, _) in tree.items():
        kids.setdefault(pid, []).append(tid)
    out, stack = [], [type_id]
    while stack:
        t = stack.pop()
        out.append(t)
        stack.extend(kids.get(t, ()))
    return out


def _path(tk, type_id):
    tree = store.trees.get(tk, {})
    names, t, guard = [], type_id, 0
    while t and t in tree and guard < 64:
        pid, name = tree[t]
        names.append({"type_id": t, "name": name})
        t, guard = pid, guard + 1
    return names[::-1]


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/api/health")
def health():
    return {"ok": True, "queue": store.q.qsize(), "writer_alive": store.thread.is_alive()}


@app.get("/api/tests")
def tests():
    rows = store.reader().execute(
        "SELECT t.test_id, s.* FROM tests t JOIN test_stats s USING(test_key) ORDER BY s.last_ms DESC").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/overview")
def overview(test: str = None):
    tk = _tk(test)
    c = store.reader()
    s = dict(c.execute("SELECT * FROM test_stats WHERE test_key=?", (tk,)).fetchone())
    s["test_id"] = store.test_ids.get(tk)
    now = int(time.time() * 1000)
    rate = c.execute(
        "SELECT committed_ms/1000 AS sec, SUM(n) AS n, COUNT(*) AS b FROM batches"
        " WHERE test_key=? AND committed_ms>=? GROUP BY sec ORDER BY sec", (tk, now - 180000)).fetchall()
    tree = store.trees.get(tk, {})
    counts = dict(c.execute("SELECT type_id, n FROM type_counts WHERE test_key=?", (tk,)).fetchall())
    # roll counts up to the first level under the root
    top = {}
    for tid, n in counts.items():
        p = _path(tk, tid)
        key = p[1]["name"] if len(p) > 1 else (p[0]["name"] if p else f"#{tid}")
        top[key] = top.get(key, 0) + n
    lat = sorted(store.ack_lat[-5000:])
    pct = (lambda q: round(lat[min(len(lat) - 1, int(q * len(lat)))], 2)) if lat else (lambda q: None)
    db_bytes = sum(os.path.getsize(DB_PATH + suf) for suf in ("", "-wal") if os.path.exists(DB_PATH + suf))
    return {
        "stats": s,
        "types": len(tree),
        "fields": c.execute("SELECT COUNT(*) FROM fields WHERE test_key=?", (tk,)).fetchone()[0],
        "rate": [dict(r) for r in rate],
        "categories": sorted(({"name": k, "n": v} for k, v in top.items()), key=lambda x: -x["n"]),
        "writer": {
            "queue": store.q.qsize(), "groups": store.metrics["groups"], "messages": store.metrics["messages"],
            "last_commit_ms": round(store.last_commit_ms, 2), "max_commit_ms": round(store.max_commit_ms, 2),
            "ack_p50_ms": pct(0.5), "ack_p99_ms": pct(0.99),
            "http": {k: v for k, v in store.metrics.items() if k.startswith("http_")},
        },
        "db_bytes": db_bytes,
        "uptime_s": int(time.time() - started),
    }


@app.get("/api/tree")
def tree(test: str = None):
    tk = _tk(test)
    c = store.reader()
    counts = dict(c.execute("SELECT type_id, n FROM type_counts WHERE test_key=?", (tk,)).fetchall())
    revs = {r[0]: (r[1], r[2]) for r in c.execute(
        "SELECT type_id, created_rev, updated_rev FROM types WHERE test_key=?", (tk,))}
    nodes = [{"id": tid, "parent": pid, "name": name, "n": counts.get(tid, 0),
              "created_rev": revs.get(tid, (None, None))[0], "updated_rev": revs.get(tid, (None, None))[1]}
             for tid, (pid, name) in store.trees.get(tk, {}).items()]
    return {"test_id": store.test_ids[tk], "nodes": nodes}


@app.get("/api/objects")
def objects(test: str = None, type: int = None, q: str = None, before: int = None, limit: int = 50):
    tk = _tk(test)
    limit = max(1, min(limit, 500))
    where, args = ["test_key=?"], [tk]
    if type is not None:
        ids = _subtree(tk, type)
        where.append(f"type_id IN ({','.join('?' * len(ids))})")
        args += ids
    if q:
        q = q.strip()
        prefix = store.test_ids[tk] + "-"
        if q.startswith(prefix) and q[len(prefix):].isdigit():
            where.append("seq=?")
            args.append(int(q[len(prefix):]))
        else:
            where.append("name LIKE ?")
            args.append(f"%{q}%")
    if before is not None:
        where.append("seq<?")
        args.append(before)
    sql = (f"SELECT seq, batch_id, type_id, name, ts_ms, frame_kind, frame_id, x, y, z FROM objects"
           f" WHERE {' AND '.join(where)} ORDER BY seq DESC LIMIT ?")
    rows = store.reader().execute(sql, args + [limit]).fetchall()
    tree = store.trees.get(tk, {})
    tid = store.test_ids[tk]
    out = [dict(r, id=f"{tid}-{r['seq']}", type_name=tree.get(r["type_id"], (0, "?"))[1]) for r in rows]
    return {"items": out, "next": out[-1]["seq"] if len(out) == limit else None}


@app.get("/api/object/{oid}")
def object_detail(oid: str):
    base, _, seq = oid.rpartition("-")
    tk = store.tests.get(base)
    if tk is None or not seq.isdigit():
        raise HTTPException(404, "unknown object")
    r = store.reader().execute("SELECT * FROM objects WHERE test_key=? AND seq=?", (tk, int(seq))).fetchone()
    if not r:
        raise HTTPException(404, "unknown object")
    d = dict(r)
    pos = None if d["frame_kind"] is None and d["x"] is None else {
        "frame_kind": d["frame_kind"], "frame_id": d["frame_id"], "unit": d["unit"], "x": d["x"], "y": d["y"], "z": d["z"]}
    return {"id": oid, "sequence": d["seq"], "type_id": d["type_id"], "type_path": _path(tk, d["type_id"]),
            "name": d["name"], "timestamp_ms": d["ts_ms"], "position": pos, "batch_id": d["batch_id"],
            "attributes": orjson.loads(d["attrs"])}


@app.get("/api/fields")
def fields(test: str = None, type: int = None, limit: int = 500):
    tk = _tk(test)
    c = store.reader()
    if type is not None:
        ids = _subtree(tk, type)
        rows = c.execute(f"SELECT * FROM fields WHERE test_key=? AND type_id IN ({','.join('?' * len(ids))})"
                         f" ORDER BY first_seq DESC LIMIT ?", [tk] + ids + [limit]).fetchall()
    else:
        rows = c.execute("SELECT * FROM fields WHERE test_key=? ORDER BY first_seq DESC LIMIT ?", (tk, limit)).fetchall()
    tree = store.trees.get(tk, {})
    return [dict(r, type_name=tree.get(r["type_id"], (0, "?"))[1]) for r in rows]


@app.get("/api/changes")
def changes(test: str = None, limit: int = 300):
    tk = _tk(test)
    c = store.reader()
    revs = [dict(r) for r in c.execute(
        "SELECT revision, n_changes, committed_ms FROM tree_revisions WHERE test_key=? ORDER BY revision DESC LIMIT ?",
        (tk, limit))]
    ch = [dict(r) for r in c.execute(
        "SELECT revision, idx, op, type_id, parent_id, name, old_parent, old_name FROM tree_changes"
        " WHERE test_key=? AND op!='create' ORDER BY revision DESC, idx LIMIT ?", (tk, limit))]
    return {"revisions": revs, "changes": ch}


@app.get("/api/map")
def sky_map(test: str = None, frame: str = "galactic", type: int = None, limit: int = 20000):
    tk = _tk(test)
    limit = max(1, min(limit, 100000))
    where, args = ["test_key=?", "frame_kind=?"], [tk, frame]
    if type is not None:
        ids = _subtree(tk, type)
        where.append(f"type_id IN ({','.join('?' * len(ids))})")
        args += ids
    rows = store.reader().execute(
        f"SELECT seq, type_id, x, y, z FROM objects WHERE {' AND '.join(where)} ORDER BY seq DESC LIMIT ?",
        args + [limit]).fetchall()
    frames = [r[0] for r in store.reader().execute(
        "SELECT DISTINCT frame_kind FROM objects WHERE test_key=? AND frame_kind IS NOT NULL LIMIT 20", (tk,))]
    return {"frames": frames, "points": [list(r) for r in rows]}


@app.get("/api/rejects")
def rejects(test: str = None, limit: int = 100):
    c = store.reader()
    tid = store.test_ids[_tk(test)]
    rows = c.execute("SELECT id, test_id, kind, batch_id, revision, code, reason, bytes, received_ms,"
                     " substr(sample,1,600) AS sample FROM rejects WHERE test_id=? OR test_id IS NULL"
                     " ORDER BY id DESC LIMIT ?", (tid, limit)).fetchall()
    return [dict(r) for r in rows]


def _bits(s):
    if not s:
        return set()
    b = base64.b64decode(s)
    return {i * 8 + k for i, x in enumerate(b) if x for k in range(8) if x >> k & 1}


@app.post("/api/reconcile")
async def reconcile(request: Request):
    """Compare a generator receipt with what is durably stored."""
    r = orjson.loads(await request.body())
    cfg = r.get("config", {})
    test_id = cfg.get("test_id")
    tk = store.tests.get(test_id)
    if tk is None:
        raise HTTPException(404, f"no data for test {test_id}")
    offset = cfg.get("max_batches", 20000)
    inj = r.get("injections", {})
    sent, acked = _bits(r.get("sent_bitmap")), _bits(r.get("ack_bitmap"))
    f_sent, f_ack, f_rej = _bits(inj.get("fault_sent_bitmap")), _bits(inj.get("fault_ack_bitmap")), _bits(inj.get("fault_rejected_bitmap"))
    d_sent, d_ack = _bits(inj.get("duplicate_sent_bitmap")), _bits(inj.get("duplicate_ack_bitmap"))
    c = store.reader()
    stored, stored_objs = set(), 0
    for bid, n in c.execute("SELECT batch_id, n FROM batches WHERE test_key=?", (tk,)):
        stored.add(bid)
        if bid < offset:
            stored_objs += n
    normal = {b for b in stored if b < offset}
    faults_stored = {b - offset for b in stored if b >= offset}
    obj_rows = c.execute("SELECT COUNT(*) FROM objects WHERE test_key=?", (tk,)).fetchone()[0]
    st = c.execute("SELECT tree_rev, dup_batches FROM test_stats WHERE test_key=?", (tk,)).fetchone()
    lost = sorted(acked - normal)
    phantom = sorted(normal - sent)
    res = {
        "test_id": test_id,
        "batches": {"sent": len(sent), "acked_by_us": len(acked), "stored": len(normal),
                    "acked_but_missing": len(lost), "acked_but_missing_ids": lost[:50],
                    "stored_never_sent": len(phantom),
                    "stored_without_ack": len(normal & (sent - acked))},
        "objects": {"generator_acknowledged": r.get("stats", {}).get("acknowledged_objects"),
                    "rows_in_store": obj_rows, "sum_of_stored_batches": stored_objs},
        "faults": {"sent": len(f_sent), "rejected": len(f_rej), "acked": len(f_ack),
                   "stored": len(faults_stored)},
        "duplicates": {"sent": len(d_sent), "acked": len(d_ack), "absorbed_by_store": st[1]},
        "tree": {"generator_attempted": r.get("tree", {}).get("attempted_revision"),
                 "generator_acknowledged": r.get("tree", {}).get("acknowledged_revision"), "stored": st[0]},
    }
    res["ok"] = (not lost and not phantom and not faults_stored and len(f_ack) == 0
                 and obj_rows == stored_objs and res["tree"]["stored"] >= (res["tree"]["generator_acknowledged"] or 0))
    return res
