"""Store-level regression tests for acknowledgement semantics (no pytest needed).
    python -I tests/regression.py REPO_DIR
Each case uses a fresh temporary database.
"""
import hashlib
import os
import sys
import tempfile
import threading

sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import orjson  # noqa: E402
from app import validate  # noqa: E402
from app.store import Job, Store  # noqa: E402

T = "t1"


def digest(m):
    return hashlib.blake2b(orjson.dumps(m, option=orjson.OPT_SORT_KEYS), digest_size=16).digest()


def send(st, m, wait=True):
    kind = m["kind"]
    err = (validate.check_objects if kind == "objects" else validate.check_types)(m, None, None)
    if err:
        return 422, err
    ev, out = threading.Event(), {}
    j = Job(kind, m, len(orjson.dumps(m)), len(m.get("objects", [1])), digest(m))
    j.done = lambda c, d: (out.update(r=(c, d)), ev.set())
    st.submit_nowait(j)
    if not wait:
        return ev, out
    ev.wait(10)
    return out["r"]


def tree(rev, changes):
    return {"kind": "types", "test_id": T, "revision": rev,
            "changes": [{"op": op, "node": {"type_id": t, "parent_id": p, "name": n}} for op, t, p, n in changes]}


def obj(seq, type_id=2, name=None, attrs=None):
    return {"id": f"{T}-{seq}", "sequence": seq, "type_id": type_id, "name": name or f"obj {seq}",
            "timestamp_ms": 1792627200000 + seq, "position": {"frame_kind": "galactic", "frame_id": "g0", "unit": "ly",
                                                              "x": seq, "y": 1, "z": 2},
            "attributes": attrs if attrs is not None else {"a": 1}}


def batch(bid, objs):
    return {"kind": "objects", "test_id": T, "batch_id": bid, "tree_revision": 1, "objects": objs}


def fresh():
    st = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    assert send(st, tree(1, [("create", 1, 0, "root"), ("create", 2, 1, "A"), ("create", 3, 1, "B")]))[0] == 200
    return st


def q(st, sql, *a):
    return st.conn.execute(sql, a).fetchall()


def stats(st):
    return dict(zip(["objects", "batches", "dup_batches", "rejected", "id_conflicts"],
                    q(st, "SELECT objects, batches, dup_batches, rejected, id_conflicts FROM test_stats")[0]))


CASES = []


def case(f):
    CASES.append(f)
    return f


@case
def identical_retry_is_idempotent(st):
    b = batch(1, [obj(1)])
    assert send(st, b) == (200, "stored")
    assert send(st, b) == (200, "duplicate")
    assert q(st, "SELECT COUNT(*) FROM objects")[0][0] == 1
    s = stats(st)
    assert s["objects"] == 1 and s["batches"] == 1 and s["dup_batches"] == 1


@case
def key_order_does_not_break_duplicate_detection(st):
    b = batch(1, [obj(1, attrs={"a": 1, "b": 2})])
    assert send(st, b)[0] == 200
    b2 = batch(1, [obj(1, attrs={"b": 2, "a": 1})])
    assert send(st, b2) == (200, "duplicate")


@case
def same_batch_id_different_content_is_rejected(st):
    assert send(st, batch(1, [obj(1)]))[0] == 200
    code, why = send(st, batch(1, [obj(2)]))
    assert code == 409, (code, why)
    assert q(st, "SELECT seq FROM objects") == [(1,)]
    assert stats(st)["rejected"] == 1


@case
def cross_batch_object_conflict_rolls_back_whole_message(st):
    assert send(st, batch(1, [obj(1, type_id=2, name="original")]))[0] == 200
    code, why = send(st, batch(2, [obj(5), obj(1, type_id=3, name="changed", attrs={"new_field": True})]))
    assert code == 409, (code, why)
    assert q(st, "SELECT batch_id FROM batches ORDER BY batch_id") == [(1,)]       # batch 2 not registered
    assert q(st, "SELECT seq FROM objects ORDER BY seq") == [(1,)]                 # obj 5 rolled back too
    assert q(st, "SELECT type_id, n FROM type_counts") == [(2, 1)]                 # counts unchanged
    assert q(st, "SELECT name FROM fields WHERE name='new_field'") == []           # no phantom field
    assert q(st, "SELECT name FROM objects WHERE seq=1") == [("original",)]


@case
def cross_batch_identical_object_is_kept_once(st):
    assert send(st, batch(1, [obj(1)]))[0] == 200
    assert send(st, batch(2, [obj(1), obj(2)])) == (200, "stored")
    assert q(st, "SELECT COUNT(*) FROM objects")[0][0] == 2
    s = stats(st)
    assert s["objects"] == 2 and s["id_conflicts"] == 1


@case
def unknown_type_is_rejected(st):
    code, why = send(st, batch(1, [obj(1, type_id=99)]))
    assert code == 422 and "unknown type_id" in why
    assert q(st, "SELECT COUNT(*) FROM objects")[0][0] == 0


@case
def stale_tree_revision_does_not_override_newer_state(st):
    assert send(st, tree(3, [("rename", 2, 1, "newest")]))[0] == 200
    assert send(st, tree(2, [("rename", 2, 1, "stale")]))[0] == 200
    assert q(st, "SELECT name, updated_rev FROM types WHERE type_id=2") == [("newest", 3)]
    assert q(st, "SELECT applied FROM tree_changes WHERE revision=2") == [(0,)]
    assert st.trees[1][2][1] == "newest"


@case
def tree_revision_conflict_is_rejected(st):
    assert send(st, tree(2, [("rename", 2, 1, "x")]))[0] == 200
    assert send(st, tree(2, [("rename", 2, 1, "x")])) == (200, "duplicate")
    assert send(st, tree(2, [("rename", 2, 1, "y")]))[0] == 409


@case
def tree_cycle_is_rejected(st):
    assert send(st, tree(2, [("create", 4, 2, "A1")]))[0] == 200
    code, why = send(st, tree(3, [("move", 2, 4, "A")]))
    assert code == 422 and "cycle" in why
    assert st.trees[1][2][0] == 1


@case
def bad_message_in_group_does_not_affect_neighbours(st):
    assert send(st, batch(1, [obj(1)]))[0] == 200
    waits = [send(st, batch(10 + i, [obj(100 + i)]), wait=False) for i in range(5)]
    waits.append(send(st, batch(20, [obj(1, name="conflict")]), wait=False))
    waits += [send(st, batch(30 + i, [obj(200 + i)]), wait=False) for i in range(5)]
    codes = []
    for ev, out in waits:
        ev.wait(10)
        codes.append(out["r"][0])
    assert codes == [200] * 5 + [409] + [200] * 5, codes
    assert q(st, "SELECT COUNT(*) FROM objects")[0][0] == 11


@case
def bool_vs_int_attribute_is_a_conflict(st):
    assert send(st, batch(1, [obj(1, attrs={"flag": True, "n": {"deep": True}})]))[0] == 200
    code, _ = send(st, batch(2, [obj(1, attrs={"flag": 1, "n": {"deep": True}})]))
    assert code == 409, code
    code, _ = send(st, batch(3, [obj(1, attrs={"flag": True, "n": {"deep": 1}})]))
    assert code == 409, code
    a = q(st, "SELECT attrs FROM object_attrs")[0][0]
    assert orjson.loads(a) == {"flag": True, "n": {"deep": True}} and orjson.loads(a)["flag"] is True


@case
def identical_then_conflict_leaves_counters_untouched(st):
    assert send(st, batch(1, [obj(1), obj(2)]))[0] == 200
    code, _ = send(st, batch(2, [obj(1), obj(2, name="changed")]))   # obj1 identical, obj2 conflicts
    assert code == 409, code
    s = stats(st)
    assert s["id_conflicts"] == 0 and s["objects"] == 2 and s["batches"] == 1, s


@case
def stale_create_after_delete_does_not_resurrect(st):
    assert send(st, tree(3, [("delete", 3, 1, "B")]))[0] == 200
    assert 3 not in st.trees[1]
    assert send(st, tree(2, [("create", 3, 1, "zombie")])) == (200, "stored")   # late, older revision
    assert 3 not in st.trees[1]
    assert q(st, "SELECT applied FROM tree_changes WHERE revision=2") == [(0,)]
    assert send(st, tree(4, [("rename", 3, 1, "late rename")]))[0] == 422      # newer change to a dead node
    assert send(st, tree(5, [("create", 3, 1, "reborn")]))[0] == 200            # newer create revives it
    assert st.trees[1][3] == (1, "reborn")
    st.close()
    st2 = Store(st.path)                                                        # tombstones survive restart
    assert st2.trees[1][3] == (1, "reborn") and st2.node_revs[1][3] == 5
    st2.close()


@case
def sequence_outside_key_range_is_rejected(st):
    o = obj(1)
    o["sequence"] = 2 ** 40
    o["id"] = f"{T}-{2 ** 40}"
    code, why = send(st, batch(1, [o]))
    assert code == 422 and "sequence" in why, (code, why)


@case
def v1_database_is_migrated_in_place(st):
    import sqlite3
    path = os.path.join(tempfile.mkdtemp(), "v1.db")
    c = sqlite3.connect(path)
    c.executescript("""
      CREATE TABLE tests(test_key INTEGER PRIMARY KEY, test_id TEXT UNIQUE NOT NULL, created_ms INTEGER);
      CREATE TABLE objects(test_key INTEGER, seq INTEGER, batch_id INTEGER, type_id INTEGER, name TEXT,
        ts_ms INTEGER, frame_kind TEXT, frame_id TEXT, unit TEXT, x REAL, y REAL, z REAL, attrs BLOB);
      CREATE UNIQUE INDEX objects_pk ON objects(test_key, seq);
      INSERT INTO tests VALUES(1, 'old', 0);
      INSERT INTO objects VALUES(1, 7, 0, 2, 'legacy', 0, NULL, NULL, NULL, NULL, NULL, NULL, '{"k":true}');
    """)
    c.commit()
    c.close()
    st2 = Store(path)
    cols = [r[1] for r in st2.conn.execute("PRAGMA table_info(objects)")]
    assert "attrs" not in cols, cols
    assert q(st2, "SELECT name FROM objects WHERE seq=7") == [("legacy",)]
    assert orjson.loads(q(st2, "SELECT attrs FROM object_attrs")[0][0]) == {"k": True}
    st2.close()


# ---------------------------------------------------------------- reconcile
def b64bits(ids):
    import base64
    ids = list(ids)
    b = bytearray((max(ids) // 8 + 1) if ids else 1)
    for i in ids:
        b[i >> 3] |= 1 << (i & 7)
    return base64.b64encode(bytes(b)).decode()


def receipt(sent, acked, fault_sent=(), fault_rej=(), dup=(), tree_ack=1, acked_objs=None, state="completed"):
    return {"config": {"test_id": T, "max_batches": 20000},
            "sent_bitmap": b64bits(sent), "ack_bitmap": b64bits(acked),
            "injections": {"fault_sent_bitmap": b64bits(fault_sent), "fault_ack_bitmap": b64bits(()),
                           "fault_rejected_bitmap": b64bits(fault_rej),
                           "duplicate_sent_bitmap": b64bits(dup), "duplicate_ack_bitmap": b64bits(dup)},
            "tree": {"attempted_revision": tree_ack, "acknowledged_revision": tree_ack},
            "stats": {"acknowledged_objects": acked_objs, "state": state}}


@case
def reconcile_happy_path(st):
    from app.reconcile import reconcile_receipt
    assert send(st, batch(0, [obj(1)]))[0] == 200 and send(st, batch(1, [obj(2)]))[0] == 200
    r = reconcile_receipt(st, receipt({0, 1}, {0, 1}, acked_objs=2))
    assert r["integrity_ok"] and r["exercise_complete"], r


@case
def reconcile_compares_sets_not_counts(st):
    from app.reconcile import reconcile_receipt
    assert send(st, batch(0, [obj(1)]))[0] == 200
    r = reconcile_receipt(st, receipt({0}, {0}, fault_sent={2}, fault_rej={3}, acked_objs=1))
    assert r["integrity_ok"] and not r["exercise_complete"], r


@case
def reconcile_rejects_incomplete_receipt(st):
    from app.reconcile import ReceiptError, reconcile_receipt
    assert send(st, batch(0, [obj(1)]))[0] == 200
    bad = receipt({0}, {0}, acked_objs=1)
    del bad["ack_bitmap"]
    try:
        reconcile_receipt(st, bad)
        raise AssertionError("missing ack_bitmap accepted")
    except ReceiptError:
        pass


@case
def reconcile_huge_revision_is_cheap(st):
    import time
    from app.reconcile import reconcile_receipt
    assert send(st, batch(0, [obj(1)]))[0] == 200
    t0 = time.perf_counter()
    r = reconcile_receipt(st, receipt({0}, {0}, tree_ack=10 ** 9, acked_objs=1))
    assert time.perf_counter() - t0 < 1.0
    assert not r["integrity_ok"] and r["tree"]["gaps_in_acked_range"] == 10 ** 9 - 1, r["tree"]


@case
def restart_rebuilds_memory(st):
    assert send(st, tree(2, [("create", 4, 2, "A1")]))[0] == 200
    assert send(st, batch(1, [obj(1, type_id=4)]))[0] == 200
    path = st.path
    st.close()
    st2 = Store(path)
    assert st2.trees[1][4] == (2, "A1")
    assert send(st2, batch(1, [obj(1, type_id=4)])) == (200, "duplicate")
    assert send(st2, batch(2, [obj(2, type_id=4)])) == (200, "stored")
    st2.close()


failed = 0
for f in CASES:
    st = fresh()
    try:
        f(st)
        print(f"PASS {f.__name__}")
    except AssertionError as e:
        failed += 1
        print(f"FAIL {f.__name__}: {e}")
    finally:
        try:
            st.close()
        except Exception:
            pass
print(f"{len(CASES) - failed}/{len(CASES)} passed")
sys.exit(1 if failed else 0)
