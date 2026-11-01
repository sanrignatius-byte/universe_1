"""Durable storage: SQLite (WAL, synchronous=FULL) behind one writer thread.

Ingest contract: the HTTP handler waits until the writer has COMMITTED the
message (fsync'd WAL) before answering 2xx. The writer drains everything that
queued up while the previous commit was running into one transaction
("group commit"), so one fsync covers many messages.
"""
import logging
import os
import queue
import sqlite3
import threading
import time
from collections import Counter, defaultdict

import orjson

log = logging.getLogger("store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tests(
  test_key INTEGER PRIMARY KEY, test_id TEXT UNIQUE NOT NULL, created_ms INTEGER);
CREATE TABLE IF NOT EXISTS test_stats(
  test_key INTEGER PRIMARY KEY, objects INTEGER DEFAULT 0, batches INTEGER DEFAULT 0,
  dup_batches INTEGER DEFAULT 0, dup_objects INTEGER DEFAULT 0, rejected INTEGER DEFAULT 0,
  id_conflicts INTEGER DEFAULT 0, bytes INTEGER DEFAULT 0, tree_rev INTEGER DEFAULT 0,
  dup_tree INTEGER DEFAULT 0, first_ms INTEGER, last_ms INTEGER);
CREATE TABLE IF NOT EXISTS batches(
  test_key INTEGER, batch_id INTEGER, n INTEGER, tree_rev INTEGER, bytes INTEGER,
  committed_ms INTEGER, PRIMARY KEY(test_key, batch_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS batches_time ON batches(test_key, committed_ms);
CREATE TABLE IF NOT EXISTS objects(
  test_key INTEGER, seq INTEGER, batch_id INTEGER, type_id INTEGER, name TEXT,
  ts_ms INTEGER, frame_kind TEXT, frame_id TEXT, unit TEXT, x REAL, y REAL, z REAL,
  attrs BLOB);
CREATE UNIQUE INDEX IF NOT EXISTS objects_pk ON objects(test_key, seq);
CREATE INDEX IF NOT EXISTS objects_type ON objects(test_key, type_id, seq);
CREATE TABLE IF NOT EXISTS types(
  test_key INTEGER, type_id INTEGER, parent_id INTEGER, name TEXT,
  created_rev INTEGER, updated_rev INTEGER, PRIMARY KEY(test_key, type_id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS tree_revisions(
  test_key INTEGER, revision INTEGER, n_changes INTEGER, committed_ms INTEGER,
  PRIMARY KEY(test_key, revision)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS tree_changes(
  test_key INTEGER, revision INTEGER, idx INTEGER, op TEXT, type_id INTEGER,
  parent_id INTEGER, name TEXT, old_parent INTEGER, old_name TEXT,
  PRIMARY KEY(test_key, revision, idx)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS type_counts(
  test_key INTEGER, type_id INTEGER, n INTEGER, PRIMARY KEY(test_key, type_id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fields(
  test_key INTEGER, type_id INTEGER, name TEXT, json_type TEXT, first_seq INTEGER,
  first_batch INTEGER, first_ms INTEGER, PRIMARY KEY(test_key, type_id, name)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS rejects(
  id INTEGER PRIMARY KEY, test_id TEXT, kind TEXT, batch_id INTEGER, revision INTEGER,
  code INTEGER, reason TEXT, bytes INTEGER, received_ms INTEGER, sample TEXT);
"""

TREE_OPS = {"create", "rename", "move", "update", "delete"}


def now_ms():
    return int(time.time() * 1000)


def json_type(v):
    t = type(v)
    if t is bool:
        return "bool"
    if t is int or t is float:
        return "number"
    if t is str:
        return "string"
    if t is list:
        return "array"
    if t is dict:
        return "object"
    return "null"


class Job:
    __slots__ = ("kind", "msg", "nbytes", "cost", "done", "code", "reason", "sample", "t0")

    def __init__(self, kind, msg, nbytes, cost=1):
        self.kind = kind
        self.msg = msg
        self.nbytes = nbytes
        self.cost = cost
        self.done = None
        self.code = 0
        self.reason = ""
        self.sample = ""
        self.t0 = time.perf_counter()


class Store:
    def __init__(self, path, queue_max=4096):
        self.path = path
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.q = queue.Queue(maxsize=queue_max)
        self.conn = self._open(path)
        self.conn.executescript(SCHEMA)
        self._local = threading.local()
        self.metrics = Counter()
        self.last_commit_ms = 0.0
        self.max_commit_ms = 0.0
        self.ack_lat = []  # recent ack latencies (ms), bounded
        self._load_memory()
        self.thread = threading.Thread(target=self._run, name="writer", daemon=True)
        self.thread.start()

    # ---------- connections ----------
    @staticmethod
    def _open(path):
        c = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        # FULL: every commit fsyncs the WAL -> survives process, container and VM/power loss.
        # NORMAL (opt-in): survives process/container crash, may lose the last commits on power loss.
        sync = os.environ.get("SQLITE_SYNC", "FULL").upper()
        c.execute(f"PRAGMA synchronous={'NORMAL' if sync == 'NORMAL' else 'FULL'}")
        c.execute("PRAGMA cache_size=-262144")  # 256 MiB
        c.execute("PRAGMA temp_store=MEMORY")
        c.execute("PRAGMA wal_autocheckpoint=4000")
        return c

    def reader(self):
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, isolation_level=None,
                                check_same_thread=False, timeout=30)
            c.execute("PRAGMA query_only=1")
            c.execute("PRAGMA cache_size=-16384")  # 16 MiB per reader thread keeps worst case well under 4 GiB
            c.row_factory = sqlite3.Row
            self._local.c = c
        return c

    # ---------- in-memory state rebuilt from the DB ----------
    def _load_memory(self):
        c = self.conn
        self.tests = {tid: k for k, tid in c.execute("SELECT test_key, test_id FROM tests")}
        self.test_ids = {k: tid for tid, k in self.tests.items()}
        trees = defaultdict(dict)
        for tk, tid, pid, name in c.execute("SELECT test_key, type_id, parent_id, name FROM types"):
            trees[tk][tid] = (pid, name)
        self.trees = dict(trees)  # values are replaced, never mutated in place
        fields = defaultdict(set)
        for tk, tid, name in c.execute("SELECT test_key, type_id, name FROM fields"):
            fields[(tk, tid)].add(name)
        self.fields = fields

    # ---------- writer ----------
    def submit_nowait(self, job):
        self.q.put_nowait(job)

    def close(self):
        self.q.put(None)
        self.thread.join(timeout=30)
        self.conn.close()

    def _run(self):
        while True:
            job = self.q.get()
            if job is None:
                return
            group = [job]
            cost = job.cost
            while len(group) < 2048 and cost < 40000:
                try:
                    j = self.q.get_nowait()
                except queue.Empty:
                    break
                if j is None:
                    self._commit_group(group)
                    return
                group.append(j)
                cost += j.cost
            self._commit_group(group)

    def _commit_group(self, group):
        t0 = time.perf_counter()
        c = self.conn
        results = None
        try:
            self._reset_acc()
            c.execute("BEGIN IMMEDIATE")
            results = [self._apply(j) for j in group]
            self._flush_acc()
            c.execute("COMMIT")
        except Exception:
            log.exception("group commit failed; retrying messages one by one")
            self._safe_rollback()
            results = []
            for j in group:
                try:
                    self._reset_acc()
                    c.execute("BEGIN IMMEDIATE")
                    r = self._apply(j)
                    self._flush_acc()
                    c.execute("COMMIT")
                except Exception as e:  # message is NOT stored -> 500, generator retries
                    log.exception("message failed")
                    self._safe_rollback()
                    r = (500, f"storage error: {e}")
                results.append(r)
        dt = (time.perf_counter() - t0) * 1000
        self.last_commit_ms = dt
        self.max_commit_ms = max(self.max_commit_ms, dt)
        self.metrics["groups"] += 1
        self.metrics["messages"] += len(group)
        t1 = time.perf_counter()
        for j, (code, detail) in zip(group, results):
            self.metrics[f"http_{code}"] += 1
            self.ack_lat.append((t1 - j.t0) * 1000)
            if j.done:
                j.done(code, detail)
        if len(self.ack_lat) > 20000:
            self.ack_lat = self.ack_lat[-5000:]

    def _safe_rollback(self):
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        self._load_memory()

    # ---------- accumulators flushed once per transaction ----------
    def _reset_acc(self):
        self.acc_stats = defaultdict(Counter)
        self.acc_types = Counter()
        self.acc_rev = {}

    def _flush_acc(self):
        c = self.conn
        t = now_ms()
        for tk, s in self.acc_stats.items():
            c.execute(
                "UPDATE test_stats SET objects=objects+?, batches=batches+?, dup_batches=dup_batches+?,"
                " dup_objects=dup_objects+?, rejected=rejected+?, id_conflicts=id_conflicts+?,"
                " bytes=bytes+?, dup_tree=dup_tree+?, first_ms=COALESCE(first_ms, ?), last_ms=? WHERE test_key=?",
                (s["objects"], s["batches"], s["dup_batches"], s["dup_objects"], s["rejected"],
                 s["id_conflicts"], s["bytes"], s["dup_tree"], t, t, tk))
        if self.acc_types:
            c.executemany(
                "INSERT INTO type_counts VALUES(?,?,?) ON CONFLICT(test_key,type_id) DO UPDATE SET n=n+excluded.n",
                [(tk, tid, n) for (tk, tid), n in self.acc_types.items()])
        for tk, rev in self.acc_rev.items():
            c.execute("UPDATE test_stats SET tree_rev=MAX(tree_rev, ?) WHERE test_key=?", (rev, tk))

    def _test_key(self, test_id):
        k = self.tests.get(test_id)
        if k is None:
            cur = self.conn.execute("INSERT INTO tests(test_id, created_ms) VALUES(?,?)", (test_id, now_ms()))
            k = cur.lastrowid
            self.conn.execute("INSERT INTO test_stats(test_key) VALUES(?)", (k,))
            self.tests[test_id] = k
            self.test_ids[k] = test_id
        return k

    # ---------- apply one message inside the open transaction ----------
    def _apply(self, j):
        if j.kind == "objects":
            return self._apply_objects(j)
        if j.kind == "types":
            return self._apply_types(j)
        return self._reject(j, j.code, j.reason)

    def _reject(self, j, code, reason):
        m = j.msg if isinstance(j.msg, dict) else {}
        test_id = m.get("test_id") if type(m.get("test_id")) is str else None
        bid = m.get("batch_id") if type(m.get("batch_id")) is int else None
        rev = m.get("revision") if type(m.get("revision")) is int else None
        self.conn.execute(
            "INSERT INTO rejects(test_id, kind, batch_id, revision, code, reason, bytes, received_ms, sample)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (test_id, str(m.get("kind")), bid, rev, code, reason, j.nbytes, now_ms(), j.sample[:4000]))
        if test_id:
            self.acc_stats[self._test_key(test_id)]["rejected"] += 1
        self.metrics["rejected"] += 1
        return code, reason

    def _apply_objects(self, j):
        c = self.conn
        m = j.msg
        tk = self._test_key(m["test_id"])
        bid = m["batch_id"]
        objs = m["objects"]
        st = self.acc_stats[tk]
        if c.execute("SELECT 1 FROM batches WHERE test_key=? AND batch_id=?", (tk, bid)).fetchone():
            st["dup_batches"] += 1
            st["dup_objects"] += len(objs)
            return 200, "duplicate"
        tree = self.trees.get(tk, {})
        missing = sorted({o["type_id"] for o in objs if o["type_id"] not in tree})
        if missing:
            j.sample = orjson.dumps(m).decode()[:4000]
            return self._reject(j, 422, f"unknown type_id {missing[:5]} (tree revision {m['tree_revision']})")
        rows = []
        dumps = orjson.dumps
        for o in objs:
            p = o["position"]
            if p is None:
                fk = fi = un = x = y = z = None
            else:
                fk, fi, un = p.get("frame_kind"), p.get("frame_id"), p.get("unit")
                x, y, z = p["x"], p["y"], p["z"]
            rows.append((tk, o["sequence"], bid, o["type_id"], o["name"], o["timestamp_ms"],
                         fk, fi, un, x, y, z, dumps(o["attributes"])))
        before = c.total_changes
        c.executemany("INSERT OR IGNORE INTO objects VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        inserted = c.total_changes - before
        t = now_ms()
        c.execute("INSERT INTO batches VALUES(?,?,?,?,?,?)", (tk, bid, len(objs), m["tree_revision"], j.nbytes, t))
        st["objects"] += inserted
        st["id_conflicts"] += len(rows) - inserted
        st["batches"] += 1
        st["bytes"] += j.nbytes
        acc_types = self.acc_types
        fields = self.fields
        new_fields = []
        for o in objs:
            tid = o["type_id"]
            acc_types[(tk, tid)] += 1
            known = fields[(tk, tid)]
            a = o["attributes"]
            if len(a) > len(known) or not known.issuperset(a):
                for k, v in a.items():
                    if k not in known:
                        known.add(k)
                        new_fields.append((tk, tid, k, json_type(v), o["sequence"], bid, t))
        if new_fields:
            c.executemany("INSERT OR IGNORE INTO fields VALUES(?,?,?,?,?,?,?)", new_fields)
        return 200, "stored"

    def _apply_types(self, j):
        c = self.conn
        m = j.msg
        tk = self._test_key(m["test_id"])
        rev = m["revision"]
        if c.execute("SELECT 1 FROM tree_revisions WHERE test_key=? AND revision=?", (tk, rev)).fetchone():
            self.acc_stats[tk]["dup_tree"] += 1
            return 200, "duplicate"
        tree = dict(self.trees.get(tk, {}))
        change_rows = []
        for i, ch in enumerate(m["changes"]):
            op = ch["op"]
            n = ch["node"]
            tid, pid, name = n["type_id"], n["parent_id"], n["name"]
            old = tree.get(tid)
            if op not in TREE_OPS:
                return self._tree_reject(j, f"changes[{i}]: unknown op {op!r}")
            if op == "create" and old is not None:
                return self._tree_reject(j, f"changes[{i}]: type {tid} already exists")
            if op != "create" and old is None:
                return self._tree_reject(j, f"changes[{i}]: type {tid} does not exist")
            if op == "delete":
                if any(p == tid for p, _ in tree.values()):
                    return self._tree_reject(j, f"changes[{i}]: type {tid} still has children")
                del tree[tid]
            else:
                if pid != 0 and pid not in tree:
                    return self._tree_reject(j, f"changes[{i}]: parent {pid} does not exist")
                a = pid
                while a:
                    if a == tid:
                        return self._tree_reject(j, f"changes[{i}]: move of {tid} under {pid} creates a cycle")
                    a = tree[a][0]
                tree[tid] = (pid, name)
            change_rows.append((tk, rev, i, op, tid, pid, name,
                                old[0] if old else None, old[1] if old else None))
        for (_, _, _, op, tid, pid, name, _, _) in change_rows:
            if op == "delete":
                c.execute("DELETE FROM types WHERE test_key=? AND type_id=?", (tk, tid))
            else:
                c.execute(
                    "INSERT INTO types VALUES(?,?,?,?,?,?) ON CONFLICT(test_key,type_id) DO UPDATE SET"
                    " parent_id=excluded.parent_id, name=excluded.name, updated_rev=excluded.updated_rev",
                    (tk, tid, pid, name, rev, rev))
        c.executemany("INSERT INTO tree_changes VALUES(?,?,?,?,?,?,?,?,?)", change_rows)
        c.execute("INSERT INTO tree_revisions VALUES(?,?,?,?)", (tk, rev, len(change_rows), now_ms()))
        self.trees[tk] = tree  # replace atomically; readers never see a half-applied tree
        self.acc_rev[tk] = max(rev, self.acc_rev.get(tk, 0))
        self.acc_stats[tk]["bytes"] += j.nbytes
        return 200, "stored"

    def _tree_reject(self, j, reason):
        j.sample = orjson.dumps(j.msg).decode()[:4000]
        return self._reject(j, 422, reason)
