import asyncio
import hashlib
import logging
import os
import queue
import sys
import time
from contextlib import asynccontextmanager

import orjson
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, ORJSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import validate
from .reconcile import ReceiptError, reconcile_receipt
from .store import MAX_DB_BYTES, MIN_FREE_BYTES, Job, Store, okey

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# The writer thread blocks in SQLite (GIL released) and must win the GIL back from the busy
# event-loop thread; a 1 ms switch interval (default 5 ms) shortens each hand-off.
sys.setswitchinterval(float(os.environ.get("GIL_SWITCH_S", "0.001")))
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

_request_cost = 0
_request_count = 0


@app.middleware("http")
async def admit_bodies(request, call_next):
    """Bound active parsing/queued requests by a conservative raw-body cost estimate."""
    global _request_cost, _request_count
    if request.method != "POST" or request.url.path not in ("/ingest", "/api/reconcile"):
        return await call_next(request)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        return _resp(415, "application/json required")
    try:
        length = int(request.headers.get("content-length", MAX_BODY))
    except ValueError:
        return _resp(400, "invalid content-length")
    if length < 0 or length > MAX_BODY:
        return _resp(413, "too large")
    cost = length * 8 + 65536
    if _request_count >= 64 or _request_cost + cost > 768 * 1024 * 1024:
        return _resp(503, "request memory budget reached")
    _request_cost += cost
    _request_count += 1
    try:
        return await call_next(request)
    finally:
        _request_cost -= cost
        _request_count -= 1


async def _read_body(request, maximum):
    body = bytearray()
    declared = request.headers.get("content-length")
    limit = min(maximum, int(declared)) if declared is not None else maximum
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(413, "body exceeds limit")
        body.extend(chunk)
    return body


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
        except RuntimeError:
            return 503, "writer unavailable"
    while not fut.done():
        done, _ = await asyncio.wait([fut], timeout=1)
        if not done and not store.thread.is_alive():
            return 503, "writer unavailable"
    return fut.result()


def _resp(code, detail):
    return Response(content=orjson.dumps({"status": detail}), status_code=code, media_type="application/json")


@app.post("/ingest")
async def ingest(request: Request):
    body = await _read_body(request, MAX_BODY)
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
                err = f"unknown kind {str(kind)[:100]!r}"
            if h.get("x-flightgen-kind") is not None and h["x-flightgen-kind"] != kind:
                err = "header X-Flightgen-Kind does not match body"
            if kind == "objects" and h.get("x-flightgen-tree-revision") is not None and h["x-flightgen-tree-revision"] != str(m.get("tree_revision")):
                err = "header X-Flightgen-Tree-Revision does not match body"
    if err:
        j = Job("reject", m, len(body))
        j.code, j.reason, j.sample = code, err, body[:4000].decode("utf-8", "replace")
        code, detail = await _submit(j)
        if code >= 500:
            return _resp(code, detail)
        return _resp(j.code, err)
    cost = len(m["objects"]) if m["kind"] == "objects" else 1
    # content digest of the canonical (key-sorted) message: a re-send with the same id is
    # only a duplicate if the content is the same
    digest = hashlib.blake2b(orjson.dumps(m, option=orjson.OPT_SORT_KEYS), digest_size=16).digest()
    code, detail = await _submit(Job(m["kind"], m, len(body), cost, digest))
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
    alive = store.thread.is_alive()
    ok = alive and not store.disk["full"]
    return ORJSONResponse({"ok": ok, "queue": store.q.qsize(), "writer_alive": alive}, status_code=200 if ok else 503)


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
        node = p[1] if len(p) > 1 else (p[0] if p else {"type_id": tid, "name": f"#{tid}"})
        key = (node["type_id"], node["name"])  # the id drives the colour, identical on every page
        top[key] = top.get(key, 0) + n
    lat = sorted(store.ack_lat[-5000:])
    pct = (lambda q: round(lat[min(len(lat) - 1, int(q * len(lat)))], 2)) if lat else (lambda q: None)
    db_bytes = sum(os.path.getsize(DB_PATH + suf) for suf in ("", "-wal") if os.path.exists(DB_PATH + suf))
    return {
        "stats": s,
        "types": len(tree),
        "fields": c.execute("SELECT COUNT(*) FROM fields WHERE test_key=?", (tk,)).fetchone()[0],
        "rate": [dict(r) for r in rate],
        "categories": sorted(({"id": k[0], "name": k[1], "n": v} for k, v in top.items()), key=lambda x: -x["n"]),
        "writer": {
            "queue": store.q.qsize(), "groups": store.metrics["groups"], "messages": store.metrics["messages"],
            "last_commit_ms": round(store.last_commit_ms, 2), "max_commit_ms": round(store.max_commit_ms, 2),
            "ack_p50_ms": pct(0.5), "ack_p99_ms": pct(0.99),
            "http": {k: v for k, v in store.metrics.items() if k.startswith("http_")},
            "sync": "NORMAL" if os.environ.get("SQLITE_SYNC", "FULL").upper() == "NORMAL" else "FULL",
        },
        "db_bytes": db_bytes,
        "disk": {"free_bytes": store.disk["free_bytes"], "budget_reached": store.disk["full"],
                 "max_db_bytes": MAX_DB_BYTES, "min_free_bytes": MIN_FREE_BYTES},
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


SEARCH_WINDOW = 1_000_000  # name search scans at most the newest N objects of a world


@app.get("/api/objects")
def objects(test: str = None, type: int = None, q: str = None, before: int = None, limit: int = 50):
    tk = _tk(test)
    c = store.reader()
    limit = max(1, min(limit, 500))
    where, args = ["test_key=?"], [tk]
    window = None
    if type is not None:
        ids = _subtree(tk, type)
        # Plan choice: a common subtree is found fastest by walking the newest objects and
        # filtering (unary + keeps SQLite off the type index, which would sort every match);
        # a rare subtree is found fastest through the (test, type, seq) index.
        counts = dict(c.execute("SELECT type_id, n FROM type_counts WHERE test_key=?", (tk,)).fetchall())
        total = sum(counts.values()) or 1
        share = sum(counts.get(i, 0) for i in ids) / total
        col = "+type_id" if share > 0.02 and len(ids) > 1 else "type_id"
        where.append(f"{col} IN ({','.join('?' * len(ids))})")
        args += ids
    if q:
        q = q.strip()
        prefix = store.test_ids[tk] + "-"
        if q.startswith(prefix) and q[len(prefix):].isdigit():
            where.append("seq=?")
            args.append(int(q[len(prefix):]))
        else:
            maxseq = c.execute("SELECT MAX(seq) FROM objects WHERE test_key=?", (tk,)).fetchone()[0] or 0
            window = SEARCH_WINDOW
            where += ["seq>?", "name LIKE ?"]
            args += [maxseq - SEARCH_WINDOW, f"%{q}%"]
    if before is not None:
        where.append("seq<?")
        args.append(before)
    sql = (f"SELECT seq, batch_id, type_id, name, ts_ms, frame_kind, frame_id, x, y, z FROM objects"
           f" WHERE {' AND '.join(where)} ORDER BY seq DESC LIMIT ?")
    rows = c.execute(sql, args + [limit]).fetchall()
    tree = store.trees.get(tk, {})
    tid = store.test_ids[tk]
    out = [dict(r, id=f"{tid}-{r['seq']}", type_name=tree.get(r["type_id"], (0, "?"))[1]) for r in rows]
    return {"items": out, "next": out[-1]["seq"] if len(out) == limit else None, "search_window": window}


@app.get("/api/object/{oid}")
def object_detail(oid: str):
    base, _, seq = oid.rpartition("-")
    tk = store.tests.get(base)
    if tk is None or not seq.isdigit():
        raise HTTPException(404, "unknown object")
    c = store.reader()
    r = c.execute("SELECT * FROM objects WHERE test_key=? AND seq=?", (tk, int(seq))).fetchone()
    if not r:
        raise HTTPException(404, "unknown object")
    d = dict(r)
    a = c.execute("SELECT attrs FROM object_attrs WHERE okey=?", (okey(tk, int(seq)),)).fetchone()
    d["attrs"] = a[0] if a else b"null"
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


MAP_WINDOW = 300000  # scan at most the newest N objects for one frame


@app.get("/api/map")
def sky_map(test: str = None, kind: str = None, frame_id: str = None, unit: str = None,
            type: int = None, limit: int = 20000):
    """Points of ONE reference frame (kind + frame_id + unit). Different local frames
    have no known transform between them, so they are never mixed in one plot."""
    tk = _tk(test)
    c = store.reader()
    limit = max(1, min(limit, 100000))
    frames = [dict(r) for r in c.execute(
        "SELECT frame_kind, frame_id, unit, n FROM frames WHERE test_key=? ORDER BY n DESC LIMIT 200", (tk,))]
    if kind is None and frames:
        kind, frame_id, unit = frames[0]["frame_kind"], frames[0]["frame_id"], frames[0]["unit"]
    if kind is None:
        return {"frames": frames, "frame": None, "points": []}
    maxseq = c.execute("SELECT MAX(seq) FROM objects WHERE test_key=?", (tk,)).fetchone()[0] or 0
    where = ["test_key=?", "seq>?", "frame_kind=?", "frame_id IS ?", "unit IS ?"]
    args = [tk, maxseq - MAP_WINDOW, kind, frame_id, unit]
    if type is not None:
        ids = _subtree(tk, type)
        where.append(f"type_id IN ({','.join('?' * len(ids))})")
        args += ids
    rows = c.execute(
        f"SELECT seq, type_id, x, y, z FROM objects WHERE {' AND '.join(where)} ORDER BY seq DESC LIMIT ?",
        args + [limit]).fetchall()
    return {"frames": frames, "frame": {"frame_kind": kind, "frame_id": frame_id, "unit": unit},
            "window": MAP_WINDOW, "points": [list(r) for r in rows]}


@app.get("/api/rejects")
def rejects(test: str = None, limit: int = 100):
    c = store.reader()
    tid = store.test_ids[_tk(test)]
    rows = c.execute("SELECT id, test_id, kind, batch_id, revision, code, reason, bytes, received_ms,"
                     " substr(sample,1,600) AS sample FROM rejects WHERE test_id=? OR test_id IS NULL"
                     " ORDER BY id DESC LIMIT ?", (tid, limit)).fetchall()
    return [dict(r) for r in rows]


MAX_RECEIPT_BYTES = 64 * 1024 * 1024


@app.post("/api/reconcile")
async def reconcile(request: Request):
    """Compare a generator receipt with what is durably stored (see app/reconcile.py)."""
    body = await _read_body(request, MAX_RECEIPT_BYTES)
    if len(body) > MAX_RECEIPT_BYTES:
        raise HTTPException(413, "receipt too large")
    try:
        r = orjson.loads(body)
    except orjson.JSONDecodeError as e:
        raise HTTPException(422, f"receipt is not valid JSON: {e}")
    try:
        return await asyncio.to_thread(reconcile_receipt, store, r)
    except ReceiptError as e:
        raise HTTPException(422, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))
