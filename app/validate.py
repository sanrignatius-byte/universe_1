"""Structural validation done in the request handler, before anything is queued.

Rules were derived from observed generator traffic (see README "Data contract").
A message that breaks any rule is rejected as a whole with 422: a 2xx would
claim the whole message was stored, which we refuse to do for broken data.
"""


MAX_SEQUENCE = 1 << 40  # object keys are (test_key << 40) | sequence


def _int(v):
    return type(v) is int


def _num(v):
    return type(v) is int or type(v) is float


def check_objects(m, h_test, h_batch):
    test_id = m.get("test_id")
    if type(test_id) is not str or not test_id:
        return "test_id missing"
    if h_test is not None and h_test != test_id:
        return "header X-Flightgen-Test-Id does not match body"
    bid = m.get("batch_id")
    if not _int(bid) or bid < 0:
        return "batch_id missing or not a non-negative integer"
    if h_batch is not None and h_batch != str(bid):
        return "header X-Flightgen-Batch-Id does not match body"
    if not _int(m.get("tree_revision")):
        return "tree_revision missing or not integer"
    objs = m.get("objects")
    if type(objs) is not list or not objs:
        return "objects must be a non-empty array"
    prefix = test_id + "-"
    seen = set()
    for i, o in enumerate(objs):
        if type(o) is not dict:
            return f"objects[{i}] is not an object"
        seq = o.get("sequence")
        if not _int(seq) or seq < 0 or seq >= MAX_SEQUENCE:
            return f"objects[{i}].sequence missing or outside [0, 2^40)"
        if seq in seen:
            return f"objects[{i}].sequence {seq} repeated inside batch"
        seen.add(seq)
        if o.get("id") != prefix + str(seq):
            return f"objects[{i}].id does not match test_id-sequence"
        if not _int(o.get("type_id")):
            return f"objects[{i}].type_id missing or not integer"
        name = o.get("name")
        if type(name) is not str or not name:
            return f"objects[{i}].name missing"
        if not _int(o.get("timestamp_ms")):
            return f"objects[{i}].timestamp_ms missing or not integer"
        if "position" not in o:
            return f"objects[{i}].position missing"
        p = o["position"]
        if p is not None:
            if type(p) is not dict:
                return f"objects[{i}].position must be object or null"
            for c in ("x", "y", "z"):
                if not _num(p.get(c)):
                    return f"objects[{i}].position.{c} missing or not numeric"
            for c in ("frame_kind", "frame_id", "unit"):
                if c in p and type(p[c]) is not str:
                    return f"objects[{i}].position.{c} must be a string"
        if type(o.get("attributes")) is not dict:
            return f"objects[{i}].attributes must be an object"
    return None


def check_types(m, h_test, h_rev):
    test_id = m.get("test_id")
    if type(test_id) is not str or not test_id:
        return "test_id missing"
    if h_test is not None and h_test != test_id:
        return "header X-Flightgen-Test-Id does not match body"
    rev = m.get("revision")
    if not _int(rev) or rev < 1:
        return "revision missing or invalid"
    if h_rev is not None and h_rev != str(rev):
        return "header X-Flightgen-Tree-Revision does not match body"
    changes = m.get("changes")
    if type(changes) is not list:
        return "changes must be an array"
    for i, ch in enumerate(changes):
        if type(ch) is not dict or type(ch.get("op")) is not str:
            return f"changes[{i}] malformed"
        n = ch.get("node")
        if type(n) is not dict:
            return f"changes[{i}].node missing"
        if not _int(n.get("type_id")) or n["type_id"] < 1:
            return f"changes[{i}].node.type_id invalid"
        if not _int(n.get("parent_id")) or n["parent_id"] < 0:
            return f"changes[{i}].node.parent_id invalid"
        if type(n.get("name")) is not str or not n["name"]:
            return f"changes[{i}].node.name invalid"
    return None
