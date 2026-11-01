"""Reconcile a generator receipt with what is durably stored.

The receipt is untrusted input: every field is type-checked, bitmaps are size-capped,
and no structure is ever sized by a number taken from the receipt. Sets are Python
big integers built from the receipt's lsb0 bitmaps (bit i == batch i), so set algebra
and equality run in C; per-batch checks walk only the rows we actually stored.
"""
import base64
import binascii

MAX_BITMAP_BYTES = 16 << 20   # 128 Mi batch ids per bitmap
MAX_REVISION = 1 << 40
FIRST = 50                    # how many example ids to list for each discrepancy

BITMAPS = ("fault_sent_bitmap", "fault_ack_bitmap", "fault_rejected_bitmap",
           "duplicate_sent_bitmap", "duplicate_ack_bitmap")


class ReceiptError(ValueError):
    pass


def _need(d, key, typ, where):
    if not isinstance(d, dict) or key not in d:
        raise ReceiptError(f"{where}.{key} is missing")
    v = d[key]
    if (type(v) is not int) if typ is int else not isinstance(v, typ):
        raise ReceiptError(f"{where}.{key} must be {typ.__name__}")
    return v


def _bitmap(d, key, where):
    s = _need(d, key, str, where)
    if len(s) > MAX_BITMAP_BYTES * 4 // 3 + 4:
        raise ReceiptError(f"{where}.{key} is larger than {MAX_BITMAP_BYTES} bytes")
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        raise ReceiptError(f"{where}.{key} is not valid base64") from None


def _bits(b):
    return int.from_bytes(b, "little")


def _has(b, i):
    return (i >> 3) < len(b) and (b[i >> 3] >> (i & 7)) & 1


def _first(x):
    out = []
    while x and len(out) < FIRST:
        low = x & -x
        out.append(low.bit_length() - 1)
        x ^= low
    return out


def reconcile_receipt(store, r):
    if not isinstance(r, dict):
        raise ReceiptError("receipt must be a JSON object")
    cfg = _need(r, "config", dict, "receipt")
    test_id = _need(cfg, "test_id", str, "config")
    offset = _need(cfg, "max_batches", int, "config")
    if not 0 < offset <= 1 << 40:
        raise ReceiptError("config.max_batches out of range")
    inj = _need(r, "injections", dict, "receipt")
    tree = _need(r, "tree", dict, "receipt")
    stats = _need(r, "stats", dict, "receipt")
    gen_acked_objs = _need(stats, "acknowledged_objects", int, "stats")
    state = _need(stats, "state", str, "stats")
    tree_ack = _need(tree, "acknowledged_revision", int, "tree")
    tree_att = _need(tree, "attempted_revision", int, "tree")
    if not (0 <= tree_ack <= MAX_REVISION and 0 <= tree_att <= MAX_REVISION and gen_acked_objs >= 0):
        raise ReceiptError("tree revisions / acknowledged_objects out of range")
    sent_b, ack_b = _bitmap(r, "sent_bitmap", "receipt"), _bitmap(r, "ack_bitmap", "receipt")
    fs_b, fa_b, fr_b, ds_b, da_b = (_bitmap(inj, k, "injections") for k in BITMAPS)

    tk = store.tests.get(test_id)
    if tk is None:
        raise LookupError(f"no data for test {test_id}")
    c = store.reader()

    # stored batches -> bitsets sized by the receipt's own bitmaps (ids beyond them were never sent)
    nbytes = max(len(sent_b), len(ack_b))
    normal = bytearray(nbytes)
    fstored = bytearray(len(fs_b))
    beyond_sent = beyond_faults = 0
    stored_normal = stored_objs = acked_objs_in_store = 0
    for bid, n in c.execute("SELECT batch_id, n FROM batches WHERE test_key=?", (tk,)):
        if bid < offset:
            stored_normal += 1
            stored_objs += n
            if _has(ack_b, bid):
                acked_objs_in_store += n
            if (bid >> 3) < nbytes:
                normal[bid >> 3] |= 1 << (bid & 7)
            else:
                beyond_sent += 1
        else:
            f = bid - offset
            if (f >> 3) < len(fstored):
                fstored[f >> 3] |= 1 << (f & 7)
            else:
                beyond_faults += 1
    S, A, N = _bits(sent_b), _bits(ack_b), _bits(normal)
    FS, FA, FR, DS, DA, FST = (_bits(x) for x in (fs_b, fa_b, fr_b, ds_b, da_b, fstored))
    lost = A & ~N
    phantom = N & ~S
    faults_stored = FST.bit_count() + beyond_faults

    obj_rows = c.execute("SELECT COUNT(*) FROM objects WHERE test_key=?", (tk,)).fetchone()[0]
    st = c.execute("SELECT tree_rev, dup_batches, id_conflicts FROM test_stats WHERE test_key=?", (tk,)).fetchone()
    # tree gaps from the database: bounded by what is stored, not by the receipt's number
    have = c.execute("SELECT COUNT(*) FROM tree_revisions WHERE test_key=? AND revision BETWEEN 1 AND ?",
                     (tk, tree_ack)).fetchone()[0]
    gaps, first_gaps, expected = tree_ack - have, [], 1
    if gaps:
        for (rev,) in c.execute("SELECT revision FROM tree_revisions WHERE test_key=? AND revision BETWEEN 1 AND ?"
                                " ORDER BY revision", (tk, tree_ack)):
            while expected < rev and len(first_gaps) < FIRST:
                first_gaps.append(expected)
                expected += 1
            expected = rev + 1
            if len(first_gaps) >= FIRST:
                break
        while expected <= tree_ack and len(first_gaps) < FIRST:
            first_gaps.append(expected)
            expected += 1

    res = {
        "test_id": test_id,
        "run": {"state": state, "finish_reason": stats.get("finish_reason")},
        "batches": {"sent": S.bit_count(), "acked_by_us": A.bit_count(), "stored": stored_normal,
                    "acked_but_missing": lost.bit_count(), "acked_but_missing_ids": _first(lost),
                    "stored_never_sent": phantom.bit_count() + beyond_sent,
                    "stored_without_ack": (N & S & ~A).bit_count()},
        "objects": {"generator_acknowledged": gen_acked_objs, "stored_in_acked_batches": acked_objs_in_store,
                    "rows_in_store": obj_rows, "sum_of_stored_batches": stored_objs,
                    "identical_resends_kept_once": st[2]},
        "faults": {"sent": FS.bit_count(), "rejected": FR.bit_count(), "acked": FA.bit_count(),
                   "stored": faults_stored, "sent_but_not_rejected_ids": _first(FS & ~FR),
                   "rejected_but_not_sent_ids": _first(FR & ~FS)},
        "duplicates": {"sent": DS.bit_count(), "acked": DA.bit_count(), "absorbed_by_store": st[1]},
        "tree": {"generator_attempted": tree_att, "generator_acknowledged": tree_ack, "stored_max": st[0],
                 "gaps_in_acked_range": gaps, "first_gaps": first_gaps},
    }
    # integrity: nothing acknowledged is missing, nothing wrong was stored, counts add up
    res["integrity_ok"] = (lost == 0 and res["batches"]["stored_never_sent"] == 0 and faults_stored == 0
                           and FA == 0 and obj_rows + st[2] == stored_objs
                           and acked_objs_in_store == gen_acked_objs and gaps == 0)
    # exercise: exactly the injected faults were rejected, every duplicate acknowledged, clean finish
    res["exercise_complete"] = FR == FS and FA == 0 and DA == DS and state == "completed"
    res["ok"] = res["integrity_ok"]
    return res
