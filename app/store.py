"""Durable storage: SQLite (WAL, synchronous=FULL) behind one writer thread.

Ingest contract: the HTTP handler waits until the writer has COMMITTED the
message (fsync'd WAL) before answering 2xx. The writer drains everything that
queued up while the previous commit was running into one transaction
("group commit"), so one fsync covers many messages. Each message runs inside
its own SAVEPOINT, so one bad message is rolled back without touching the rest.
"""
import functools
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
  committed_ms INTEGER, digest BLOB, PRIMARY KEY(test_key, batch_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS batches_time ON batches(test_key, committed_ms);
CREATE TABLE IF NOT EXISTS objects(
  test_key INTEGER, seq INTEGER, batch_id INTEGER, type_id INTEGER, name TEXT,
  ts_ms INTEGER, frame_kind TEXT, frame_id TEXT, unit TEXT, x REAL, y REAL, z REAL);
-- the (large, ~1.5 KB) attribute JSON lives in its own table so that scans over
-- objects (search, map, lists) touch ~150 B rows instead of ~1.6 KB rows
CREATE TABLE IF NOT EXISTS object_attrs(okey INTEGER PRIMARY KEY, attrs BLOB);
CREATE UNIQUE INDEX IF NOT EXISTS objects_pk ON objects(test_key, seq);
CREATE INDEX IF NOT EXISTS objects_type ON objects(test_key, type_id, seq);
CREATE TABLE IF NOT EXISTS types(
  test_key INTEGER, type_id INTEGER, parent_id INTEGER, name TEXT,
  created_rev INTEGER, updated_rev INTEGER, deleted_rev INTEGER,
  PRIMARY KEY(test_key, type_id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS tree_revisions(
  test_key INTEGER, revision INTEGER, n_changes INTEGER, committed_ms INTEGER, digest BLOB,
  PRIMARY KEY(test_key, revision)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS tree_changes(
  test_key INTEGER, revision INTEGER, idx INTEGER, op TEXT, type_id INTEGER,
  parent_id INTEGER, name TEXT, old_parent INTEGER, old_name TEXT, applied INTEGER DEFAULT 1,
  PRIMARY KEY(test_key, revision, idx)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS type_counts(
  test_key INTEGER, type_id INTEGER, n INTEGER, PRIMARY KEY(test_key, type_id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS frames(
  test_key INTEGER, frame_kind TEXT, frame_id TEXT, unit TEXT, n INTEGER,
  PRIMARY KEY(test_key, frame_kind, frame_id, unit)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fields(
  test_key INTEGER, type_id INTEGER, name TEXT, json_type TEXT, first_seq INTEGER,
  first_batch INTEGER, first_ms INTEGER, PRIMARY KEY(test_key, type_id, name)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS rejects(
  id INTEGER PRIMARY KEY, test_id TEXT, kind TEXT, batch_id INTEGER, revision INTEGER,
  code INTEGER, reason TEXT, bytes INTEGER, received_ms INTEGER, sample TEXT);
"""
# columns added after the first release; ALTER is a no-op error on fresh databases
MIGRATIONS = [
    "ALTER TABLE batches ADD COLUMN digest BLOB",
    "ALTER TABLE tree_revisions ADD COLUMN digest BLOB",
    "ALTER TABLE tree_changes ADD COLUMN applied INTEGER DEFAULT 1",
    "ALTER TABLE types ADD COLUMN deleted_rev INTEGER",
]

TREE_OPS = {"create", "rename", "move", "update", "delete"}
OBJ_COLS = "type_id, name, ts_ms, frame_kind, frame_id, unit, x, y, z, batch_id"


def okey(test_key, seq):
    """Integer key of an object's attribute row: test in the high bits, sequence in the low 40."""
    return (test_key << 40) | seq


@functools.lru_cache(maxsize=64)
def _multi_insert(table, ncols, nrows):
    row = "(" + ",".join("?" * ncols) + ")"
    return f"INSERT INTO {table} VALUES " + ",".join([row] * nrows)


MULTI_ROWS = 400  # rows per multi-row INSERT (keeps parameters well under SQLite's 32766 limit)


def insert_rows(c, table, ncols, rows):
    """One multi-row INSERT per chunk instead of executemany: the sqlite3 module releases and
    re-acquires the GIL around every statement step, so executemany costs one GIL hand-off
    per row; under a busy event loop each hand-off can wait a full switch interval."""
    for i in range(0, len(rows), MULTI_ROWS):
        chunk = rows[i:i + MULTI_ROWS]
        c.execute(_multi_insert(table, ncols, len(chunk)), [v for r in chunk for v in r])

MAX_DB_BYTES = int(float(os.environ.get("MAX_DB_GIB", "17")) * (1 << 30))      # of the 20 GiB volume
MIN_FREE_BYTES = int(float(os.environ.get("MIN_FREE_GIB", "1")) * (1 << 30))   # headroom for WAL/checkpoint


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


def same_core(old, new_row):
    """Stored core columns vs a new object row. Types matter (True is not 1, "1" is not 1);
    coordinates are compared as numbers because SQLite keeps them as REAL."""
    for a, b in zip(old[:6], new_row[3:9]):        # type_id, name, ts_ms, frame_kind, frame_id, unit
        if type(a) is not type(b) or a != b:
            return False
    for a, b in zip(old[6:9], new_row[9:12]):      # x, y, z
        if (a is None) != (b is None) or (a is not None and float(a) != float(b)):
            return False
    return True


def same_json(stored_bytes, value):
    """Exact JSON equality, independent of key order: true != 1, 1 != 1.0, nested too."""
    opt = orjson.OPT_SORT_KEYS
    return orjson.dumps(orjson.loads(stored_bytes), option=opt) == orjson.dumps(value, option=opt)


class Conflict(Exception):
    """Message must not be stored; rolled back to its savepoint and rejected."""

    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code
        self.reason = reason


class Job:
    __slots__ = ("kind", "msg", "nbytes", "cost", "done", "code", "reason", "sample", "t0", "digest")

    def __init__(self, kind, msg, nbytes, cost=1, digest=None):
        self.kind = kind
        self.msg = msg
        self.nbytes = nbytes
        self.cost = cost
        self.digest = digest
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
        self._migrate_v1()
        self.conn.executescript(SCHEMA)
        for sql in MIGRATIONS:
            try:
                self.conn.execute(sql)
            except sqlite3.OperationalError:
                pass
        self._local = threading.local()
        self.metrics = Counter()
        self.last_commit_ms = 0.0
        self.max_commit_ms = 0.0
        self.ack_lat = []  # recent ack latencies (ms), bounded
        self.disk = {"db_bytes": 0, "free_bytes": None, "full": False, "checked": 0.0}
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

    def _migrate_v1(self):
        """v1 kept the attribute JSON inside `objects`. Move it to `object_attrs` in place,
        in one transaction (no data volume has to be deleted to upgrade)."""
        c = self.conn
        cols = [r[1] for r in c.execute("PRAGMA table_info(objects)")]
        if "attrs" not in cols:
            return
        log.warning("migrating %s from schema v1 (attributes inside objects)", self.path)
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("CREATE TABLE IF NOT EXISTS object_attrs(okey INTEGER PRIMARY KEY, attrs BLOB)")
            c.execute("INSERT OR IGNORE INTO object_attrs SELECT (test_key << 40) | seq, attrs FROM objects")
            c.execute("CREATE TABLE objects_v2(test_key INTEGER, seq INTEGER, batch_id INTEGER, type_id INTEGER,"
                      " name TEXT, ts_ms INTEGER, frame_kind TEXT, frame_id TEXT, unit TEXT, x REAL, y REAL, z REAL)")
            c.execute("INSERT INTO objects_v2 SELECT test_key, seq, batch_id, type_id, name, ts_ms, frame_kind,"
                      " frame_id, unit, x, y, z FROM objects ORDER BY rowid")
            c.execute("DROP TABLE objects")  # drops its indexes; SCHEMA recreates them
            c.execute("ALTER TABLE objects_v2 RENAME TO objects")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    # ---------- in-memory state rebuilt from the DB ----------
    def _load_memory(self):
        c = self.conn
        self.tests = {tid: k for k, tid in c.execute("SELECT test_key, test_id FROM tests")}
        self.test_ids = {k: tid for tid, k in self.tests.items()}
        trees, revs = defaultdict(dict), defaultdict(dict)
        for tk, tid, pid, name, urev, drev in c.execute(
                "SELECT test_key, type_id, parent_id, name, updated_rev, deleted_rev FROM types"):
            if drev is None:
                trees[tk][tid] = (pid, name)
            revs[tk][tid] = urev  # for a deleted node this is the delete revision (tombstone)
        self.trees = dict(trees)      # live nodes; values are replaced, never mutated in place
        self.node_revs = dict(revs)   # revision that last changed each node, deleted ones included
        fields = defaultdict(set)
        for tk, tid, name in c.execute("SELECT test_key, type_id, name FROM fields"):
            fields[(tk, tid)].add(name)
        self.fields = fields

    # ---------- disk budget ----------
    def _check_disk(self):
        now = time.monotonic()
        if now - self.disk["checked"] < 1.0:
            return self.disk["full"]
        db = sum(os.path.getsize(self.path + s) for s in ("", "-wal", "-shm") if os.path.exists(self.path + s))
        try:
            st = os.statvfs(os.path.dirname(self.path) or ".")
            free = st.f_bavail * st.f_frsize
        except (AttributeError, OSError):
            free = None
        full = db > MAX_DB_BYTES or (free is not None and free < MIN_FREE_BYTES)
        if full and not self.disk["full"]:
            log.error("disk budget reached (db=%s free=%s): refusing new writes with 507", db, free)
        self.disk.update(db_bytes=db, free_bytes=free, full=full, checked=now)
        return full

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
        if self._check_disk():  # never acknowledge what we may not be able to keep
            results = [(507, "storage budget reached") for _ in group]
        else:
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
        self.acc_frames = Counter()
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
        if self.acc_frames:
            c.executemany(
                "INSERT INTO frames VALUES(?,?,?,?,?) ON CONFLICT(test_key,frame_kind,frame_id,unit)"
                " DO UPDATE SET n=n+excluded.n",
                [(tk, fk, fi, un, n) for (tk, fk, fi, un), n in self.acc_frames.items()])
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
        if j.kind not in ("objects", "types"):
            return self._reject(j, j.code, j.reason)
        c = self.conn
        tk = self._test_key(j.msg["test_id"])  # outside the savepoint: memory and row stay in sync
        fn = self._apply_objects if j.kind == "objects" else self._apply_types
        c.execute("SAVEPOINT msg")
        try:
            try:
                r = fn(j, tk, False)
            except sqlite3.IntegrityError:  # an object id already exists: decide row by row
                c.execute("ROLLBACK TO msg")
                r = fn(j, tk, True)
            c.execute("RELEASE msg")
            return r
        except Conflict as e:
            c.execute("ROLLBACK TO msg")
            c.execute("RELEASE msg")
            j.sample = orjson.dumps(j.msg).decode()[:4000]
            return self._reject(j, e.code, e.reason)

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

    def _apply_objects(self, j, tk, slow):
        c = self.conn
        m = j.msg
        bid = m["batch_id"]
        objs = m["objects"]
        st = self.acc_stats[tk]
        row = c.execute("SELECT digest FROM batches WHERE test_key=? AND batch_id=?", (tk, bid)).fetchone()
        if row:
            if row[0] is not None and j.digest is not None and row[0] != j.digest:
                raise Conflict(409, f"batch {bid} was already stored with different content")
            st["dup_batches"] += 1
            st["dup_objects"] += len(objs)
            return 200, "duplicate"
        tree = self.trees.get(tk, {})
        missing = sorted({o["type_id"] for o in objs if o["type_id"] not in tree})
        if missing:
            raise Conflict(422, f"unknown type_id {missing[:5]} (tree revision {m['tree_revision']})")
        rows, arows = [], []
        dumps = orjson.dumps
        base = tk << 40
        for o in objs:
            p = o["position"]
            if p is None:
                fk = fi = un = x = y = z = None
            else:
                fk, fi, un = p.get("frame_kind"), p.get("frame_id"), p.get("unit")
                x, y, z = p["x"], p["y"], p["z"]
            seq = o["sequence"]
            rows.append((tk, seq, bid, o["type_id"], o["name"], o["timestamp_ms"], fk, fi, un, x, y, z))
            arows.append((base | seq, dumps(o["attributes"])))
        ins = "INSERT INTO objects VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
        ains = "INSERT INTO object_attrs VALUES(?,?)"
        identical = 0  # message-local: merged into the totals only once the whole message is stored
        if not slow:
            insert_rows(c, "objects", 12, rows)  # IntegrityError -> caller retries with slow=True
            insert_rows(c, "object_attrs", 2, arows)
            accepted = list(zip(objs, rows))
        else:
            accepted = []
            for o, r, ar in zip(objs, rows, arows):
                old = c.execute(f"SELECT {OBJ_COLS} FROM objects WHERE test_key=? AND seq=?", (tk, r[1])).fetchone()
                if old is None:
                    c.execute(ins, r)
                    c.execute(ains, ar)
                    accepted.append((o, r))
                    continue
                oa = c.execute("SELECT attrs FROM object_attrs WHERE okey=?", (ar[0],)).fetchone()
                if not (same_core(old, r) and oa is not None and same_json(oa[0], o["attributes"])):
                    raise Conflict(409, f"object {o['id']} already stored by batch {old[9]} with different content")
                identical += 1  # identical re-send under a new batch id: keep one copy
        st["id_conflicts"] += identical
        t = now_ms()
        c.execute("INSERT INTO batches VALUES(?,?,?,?,?,?,?)",
                  (tk, bid, len(objs), m["tree_revision"], j.nbytes, t, j.digest))
        # statistics and field discovery only for objects actually stored by this message
        st["objects"] += len(accepted)
        st["batches"] += 1
        st["bytes"] += j.nbytes
        acc_types, acc_frames, fields = self.acc_types, self.acc_frames, self.fields
        new_fields = []
        for o, r in accepted:
            tid = o["type_id"]
            acc_types[(tk, tid)] += 1
            if r[6] is not None:
                acc_frames[(tk, r[6], r[7], r[8])] += 1
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

    def _apply_types(self, j, tk, slow):
        c = self.conn
        m = j.msg
        rev = m["revision"]
        row = c.execute("SELECT digest FROM tree_revisions WHERE test_key=? AND revision=?", (tk, rev)).fetchone()
        if row:
            if row[0] is not None and j.digest is not None and row[0] != j.digest:
                raise Conflict(409, f"tree revision {rev} was already stored with different content")
            self.acc_stats[tk]["dup_tree"] += 1
            return 200, "duplicate"
        tree = dict(self.trees.get(tk, {}))
        nrev = dict(self.node_revs.get(tk, {}))
        change_rows = []
        for i, ch in enumerate(m["changes"]):
            op = ch["op"]
            n = ch["node"]
            tid, pid, name = n["type_id"], n["parent_id"], n["name"]
            old = tree.get(tid)
            if op not in TREE_OPS:
                raise Conflict(422, f"changes[{i}]: unknown op {op!r}")
            if nrev.get(tid, 0) > rev:
                # A newer revision already set (or deleted) this node: per-node last-writer-wins.
                # The stale change is recorded but not applied, so a late create cannot revive
                # a node deleted by a newer revision (tombstone) and a late rename cannot undo a newer one.
                change_rows.append((tk, rev, i, op, tid, pid, name,
                                    old[0] if old else None, old[1] if old else None, 0))
                continue
            if op == "create" and old is not None:
                raise Conflict(422, f"changes[{i}]: type {tid} already exists")
            if op != "create" and old is None:
                raise Conflict(422, f"changes[{i}]: type {tid} does not exist")
            if op == "delete":
                if any(p == tid for p, _ in tree.values()):
                    raise Conflict(422, f"changes[{i}]: type {tid} still has children")
                del tree[tid]
                nrev[tid] = rev  # tombstone
            else:
                if pid != 0 and pid not in tree:
                    raise Conflict(422, f"changes[{i}]: parent {pid} does not exist")
                a = pid
                while a:
                    if a == tid:
                        raise Conflict(422, f"changes[{i}]: move of {tid} under {pid} creates a cycle")
                    a = tree[a][0]
                tree[tid] = (pid, name)
                nrev[tid] = rev
            change_rows.append((tk, rev, i, op, tid, pid, name,
                                old[0] if old else None, old[1] if old else None, 1))
        for (_, _, _, op, tid, pid, name, _, _, applied) in change_rows:
            if not applied:
                continue
            if op == "delete":  # keep the row as a tombstone so the delete revision survives restarts
                c.execute("UPDATE types SET deleted_rev=?, updated_rev=? WHERE test_key=? AND type_id=?",
                          (rev, rev, tk, tid))
            else:
                c.execute(
                    "INSERT INTO types VALUES(?,?,?,?,?,?,NULL) ON CONFLICT(test_key,type_id) DO UPDATE SET"
                    " parent_id=excluded.parent_id, name=excluded.name, updated_rev=excluded.updated_rev,"
                    " deleted_rev=NULL", (tk, tid, pid, name, rev, rev))
        c.executemany("INSERT INTO tree_changes VALUES(?,?,?,?,?,?,?,?,?,?)", change_rows)
        c.execute("INSERT INTO tree_revisions VALUES(?,?,?,?,?)", (tk, rev, len(change_rows), now_ms(), j.digest))
        # replace atomically; readers never see a half-applied tree. On a later group
        # failure _safe_rollback() reloads both from the database.
        self.trees[tk] = tree
        self.node_revs[tk] = nrev
        self.acc_rev[tk] = max(rev, self.acc_rev.get(tk, 0))
        self.acc_stats[tk]["bytes"] += j.nbytes
        return 200, "stored"
