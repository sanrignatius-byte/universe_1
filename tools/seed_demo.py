"""Seed a demo world through /ingest so the UI can be reviewed without the generator.

Builds one test run with: a realistic category tree (plus a few later revisions
with renames/moves/new types), a galactic frame laid out as spiral arms so the
sky map has visible structure, two smaller frames, non-spatial objects, some
duplicates and a few faulty messages for the trust page.
"""
import math
import random
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8001"
TEST = sys.argv[2] if len(sys.argv) > 2 else "demo-ui"
random.seed(7)

CATS = {
    "星体": ["恒星", "行星", "卫星", "小行星", "彗星", "中子星"],
    "生物": ["原核生物", "真菌", "植物", "动物", "未知生物"],
    "航行器": ["探测器", "飞船", "空间站", "卫星平台"],
    "设备": ["望远镜", "传感器阵列", "中继站"],
    "文档": ["观测日志", "研究论文", "星图志"],
    "商品": ["矿物样本盒", "纪念品"],
    "样本": ["岩石样本", "冰芯样本", "大气样本"],
    "任务": ["普查任务", "追踪任务"],
    "观测": ["光谱观测", "射电观测"],
    "资产": ["轨道权", "频谱牌照"],
    "服务": ["数据托管", "导航服务"],
}

next_id = 1
id_of = {}


def node(parent, name):
    """Register (parent,name) -> id and return the create-change."""
    global next_id
    id_of[(parent, name)] = next_id
    ch = {"op": "create", "node": {"type_id": next_id, "parent_id": parent, "name": name}}
    next_id += 1
    return ch


def nid(parent, name):
    return id_of[(parent, name)]


def send(client, msg):
    h = {"X-Flightgen-Kind": msg["kind"], "X-Flightgen-Test-Id": TEST}
    if msg["kind"] == "objects":
        h["X-Flightgen-Batch-Id"] = str(msg.get("batch_id"))
    else:
        h["X-Flightgen-Tree-Revision"] = str(msg.get("revision"))
    return client.post(BASE + "/ingest", json=msg, headers=h).status_code


NAME_POOL = ["天枢", "天璇", "天玑", "天权", "玉衡", "开阳", "摇光", "紫微", "少微", "文昌",
             "贯索", "七公", "天棓", "织女", "河鼓", "天津", "匕星", "毕宿", "昴宿", "心宿"]


def main():
    t0 = time.time()
    with httpx.Client(timeout=60) as c:
        root = node(0, "万物")["node"]["type_id"]
        rev = 1
        changes = [node(0, "万物")]
        for cat, subs in CATS.items():
            changes.append(node(root, cat))
            for s in subs:
                changes.append(node(nid(root, cat), s))
        assert send(c, {"kind": "types", "test_id": TEST, "revision": rev, "changes": changes}) == 200

        rev += 1
        changes = [
            node(root, "褐矮星"),
            {"op": "rename", "node": {"type_id": nid(root, "未知生物"), "parent_id": nid(root, "生物"), "name": "未分类生物"}},
        ]
        assert send(c, {"kind": "types", "test_id": TEST, "revision": rev, "changes": changes}) == 200
        rev += 1
        changes = [
            node(nid(root, "观测"), "引力波观测"),
            {"op": "move", "node": {"type_id": nid(root, "纪念品"), "parent_id": nid(root, "文档"), "name": "纪念品"}},
        ]
        assert send(c, {"kind": "types", "test_id": TEST, "revision": rev, "changes": changes}) == 200

        seq = 0
        now = int(time.time() * 1000)
        GAL = ("galactic", "G-0", "ly")

        def gal_pos():
            if random.random() < 0.18:                       # central bulge
                r = abs(random.gauss(0, 2600))
                th = random.uniform(0, 6.2832)
                return r * math.cos(th), r * math.sin(th), random.gauss(0, 420)
            if random.random() < 0.06:                       # halo outliers
                r = random.uniform(30000, 52000)
                th = random.uniform(0, 6.2832)
                return r * math.cos(th), r * math.sin(th), random.uniform(-9000, 9000)
            arm = random.randint(0, 3)                       # 4 spiral arms
            t = random.uniform(0.35, 3.1)
            r = 4200 + t * 11500
            th = t + arm * 1.5708 + random.gauss(0, 0.09)
            j = abs(random.gauss(0, 700))
            return r * math.cos(th) + j, r * math.sin(th) + j * random.choice([-1, 1]), random.gauss(0, 260)

        batch, bid = [], 0

        def flush():
            nonlocal batch, bid
            if not batch:
                return
            bid += 1
            assert send(c, {"kind": "objects", "test_id": TEST, "batch_id": bid,
                            "tree_revision": rev, "objects": batch}) == 200
            batch = []

        def add(type_parent, type_name, pos, attrs):
            nonlocal seq
            seq += 1
            batch.append({"id": f"{TEST}-{seq}", "sequence": seq, "type_id": nid(type_parent, type_name),
                          "name": NAME_POOL[(seq * 7) % len(NAME_POOL)] + f"-{seq:05d}",
                          "timestamp_ms": now - (30000 - seq) * 40,
                          "position": pos, "attributes": attrs})
            if len(batch) >= 20:
                flush()

        for _ in range(5200):
            cat = random.choices(list(CATS), weights=[46, 8, 12, 6, 6, 3, 5, 4, 5, 2, 3])[0]
            if cat in ("文档", "资产", "服务"):
                add(root, cat, None, {"pages": random.randint(1, 900), "level": random.choice(["公开", "内部"])})
                continue
            x, y, z = gal_pos()
            add(root, cat, {"frame_kind": GAL[0], "frame_id": GAL[1], "unit": GAL[2],
                            "x": round(x, 1), "y": round(y, 1), "z": round(z, 1)},
                {"mass_solar": round(random.uniform(0.2, 40), 2),
                 "spectral_class": random.choice("OBAFGKM"),
                 "tags": random.sample(["普查", "追踪", "复核", "α", "β", "γ"], random.randint(1, 2))})

        SOL = ("stellar_system", "S-sol-0", "km")
        for i in range(1400):
            if i < 9:
                r = 60_000_000 * (i + 1) ** 1.6
                th = random.uniform(0, 6.2832)
                add(root, "行星", {"frame_kind": SOL[0], "frame_id": SOL[1], "unit": SOL[2],
                                   "x": round(r * math.cos(th), 1), "y": round(r * math.sin(th), 1),
                                   "z": round(random.gauss(0, r * 0.02), 1)},
                    {"orbit_km": round(r, 1), "moons": random.randint(0, 20)})
            elif i < 40:
                th = random.uniform(0, 6.2832)
                r = random.uniform(1e8, 9e8)
                add(root, "彗星", {"frame_kind": SOL[0], "frame_id": SOL[1], "unit": SOL[2],
                                   "x": round(r * math.cos(th), 1), "y": round(r * math.sin(th), 1),
                                   "z": round(random.gauss(0, 3e7), 1)},
                    {"eccentricity": round(random.uniform(0.7, 0.99), 3)})
            else:
                add(root, "探测器" if i % 2 else "飞船",
                    {"frame_kind": SOL[0], "frame_id": SOL[1], "unit": SOL[2],
                     "x": round(random.gauss(0, 4e8), 1), "y": round(random.gauss(0, 4e8), 1),
                     "z": round(random.gauss(0, 2e7), 1)},
                    {"delta_v_kms": round(random.uniform(1, 60), 2)})

        LOC = ("local_surface", "L-tdl-3", "m")
        for i in range(900):
            add(root, random.choice(["传感器阵列", "望远镜", "中继站"]),
                {"frame_kind": LOC[0], "frame_id": LOC[1], "unit": LOC[2],
                 "x": round(random.uniform(-800, 800), 1), "y": round(random.uniform(-800, 800), 1),
                 "z": round(10 + random.uniform(0, 40), 1)},
                {"status": random.choice(["在线", "在线", "在线", "维护", "休眠"])})
        flush()

        # two fixed batches re-sent verbatim -> absorbed as duplicates
        fixed = []
        s = 10_000_000
        for k in range(2):
            objs = []
            for i in range(3):
                objs.append({"id": f"{TEST}-{s}", "sequence": s, "type_id": nid(root, "恒星"),
                             "name": f"重复验证-{k}-{i}", "timestamp_ms": 1700000000000,
                             "position": {"frame_kind": "galactic", "frame_id": "G-0", "unit": "ly",
                                          "x": float(s % 100), "y": float(s % 97), "z": 1.0},
                             "attributes": {"note": "duplicate probe"}})
                s += 1
            m = {"kind": "objects", "test_id": TEST, "batch_id": 700000 + k, "tree_revision": rev, "objects": objs}
            fixed.append(m)
            assert send(c, m) == 200
        for m in fixed:
            assert send(c, m) == 200

        bad = [
            {"kind": "objects", "test_id": TEST, "batch_id": 900001, "tree_revision": rev,
             "objects": [{"id": f"{TEST}-999999001", "sequence": 999999001, "type_id": 999999,
                          "name": "未知类型", "timestamp_ms": now, "position": None, "attributes": {}}]},
            {"kind": "objects", "test_id": TEST, "batch_id": 900002, "tree_revision": rev,
             "objects": [{"id": "bad", "sequence": 999999002, "type_id": 1, "name": "id 不匹配",
                          "timestamp_ms": now, "position": None, "attributes": {}}]},
            {"kind": "objects", "test_id": TEST, "batch_id": 900003, "tree_revision": rev,
             "objects": [{"id": f"{TEST}-999999003", "sequence": 999999003, "type_id": 1, "name": "缺 position",
                          "timestamp_ms": now, "attributes": {}}]},
        ]
        for msg in bad:
            assert send(c, msg) in (400, 422)
        print(f"seeded {seq} objects in {bid} batches, rev {rev}, "
              f"{len(fixed)} duplicates, {len(bad)} rejects in {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
