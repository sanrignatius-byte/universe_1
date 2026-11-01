"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = (n) => (n == null ? "–" : Number(n).toLocaleString("zh-CN"));
const bytes = (n) => (n > 1 << 30 ? (n / (1 << 30)).toFixed(2) + " GiB" : (n / (1 << 20)).toFixed(1) + " MiB");
const ts = (ms) => (ms ? new Date(ms).toISOString().replace("T", " ").slice(0, 19) : "–");
const PALETTE = ["#6ea8fe", "#3ecf8e", "#f5b84b", "#ff6b6b", "#c084fc", "#2dd4bf", "#f472b6", "#a3e635", "#fb923c", "#60a5fa", "#e879f9", "#facc15", "#94a3b8"];

const S = { test: null, tab: "overview", tree: null, sel: null, next: null, mapPts: [], charts: {} };

async function api(path, opt) {
  const r = await fetch(path, opt);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}
const qs = (o) => Object.entries(o).filter(([, v]) => v != null && v !== "").map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");

function chart(id) {
  if (!S.charts[id]) S.charts[id] = echarts.init(document.getElementById(id), "dark", { renderer: "canvas" });
  return S.charts[id];
}
window.addEventListener("resize", () => { Object.values(S.charts).forEach((c) => c.resize()); if (S.tab === "map") drawMap(); });

// ---------------------------------------------------------------- tabs & test selector
document.querySelectorAll("#tabs button").forEach((b) => b.onclick = () => {
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("on", x === b));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.id === b.dataset.tab));
  S.tab = b.dataset.tab;
  document.querySelector("main").classList.toggle("full", S.tab === "map");
  refresh(true);
});

async function loadTests() {
  try {
    const tests = await api("/api/tests");
    const sel = $("#test");
    const cur = sel.value;
    sel.innerHTML = tests.map((t) => `<option value="${esc(t.test_id)}">${esc(t.test_id)} · ${fmt(t.objects)} 对象</option>`).join("");
    if (cur && tests.some((t) => t.test_id === cur)) sel.value = cur;
    S.test = sel.value || null;
  } catch (e) { /* no data yet */ }
}
$("#test").onchange = () => { S.test = $("#test").value; S.tree = null; S.sel = null; M.frame = null; M.view = null; M.sel = null; M.hidden.clear(); refresh(true); };

// ---------------------------------------------------------------- tree helpers
async function loadTree(force) {
  if (S.tree && !force && !S.treeStale) return S.tree;
  const d = await api("/api/tree?" + qs({ test: S.test }));
  const by = new Map(), kids = new Map();
  d.nodes.forEach((n) => { by.set(n.id, n); if (!kids.has(n.parent)) kids.set(n.parent, []); kids.get(n.parent).push(n.id); });
  const total = new Map();
  const tot = (id) => { if (total.has(id)) return total.get(id); let s = by.get(id)?.n || 0; (kids.get(id) || []).forEach((k) => s += tot(k)); total.set(id, s); return s; };
  const roots = kids.get(0) || [];
  roots.forEach(tot);
  // top-level category (child of root) for colouring
  const topOf = new Map();
  const tops = roots.flatMap((r) => kids.get(r) || []);
  const walk = (id, top) => { topOf.set(id, top); (kids.get(id) || []).forEach((k) => walk(k, top)); };
  tops.forEach((t) => walk(t, t));
  roots.forEach((r) => topOf.set(r, r));
  // replace the whole tree only after a successful load: colours and labels never fall back mid-refresh
  S.tree = { by, kids, total, roots, tops, topOf, maxRev: Math.max(0, ...d.nodes.map((n) => n.created_rev || 0)) };
  S.treeStale = false;
  return S.tree;
}
// one colour per first-level category, derived from its id: identical on every page and after every refresh
const catColor = (top) => PALETTE[((top % PALETTE.length) + PALETTE.length) % PALETTE.length];
const topId = (tid) => S.tree?.topOf.get(tid) ?? tid;
const typeColor = (tid) => catColor(topId(tid));

// ---------------------------------------------------------------- overview
async function renderOverview() {
  const o = await api("/api/overview?" + qs({ test: S.test }));
  const s = o.stats;
  const last = o.rate.length ? o.rate[o.rate.length - 1] : null;
  const recent = o.rate.filter((r) => r.sec >= Date.now() / 1000 - 6);
  const nowRate = recent.length ? Math.round(recent.reduce((a, r) => a + r.n, 0) / Math.max(1, recent.length)) : 0;
  const w0 = o.writer;
  const primary = [
    ["已落盘对象", fmt(s.objects), "good"],
    ["当前摄入（对象/秒）", fmt(nowRate)],
    ["确认延迟 p50 / p99", `${w0.ack_p50_ms ?? "–"} / ${w0.ack_p99_ms ?? "–"} ms`],
    ["数据把关：拒收故障 · 吸收重复", `${fmt(s.rejected)} · ${fmt(s.dup_batches)}`, s.rejected || s.dup_batches ? "warn" : ""],
  ];
  const secondary = [
    ["已确认批次", fmt(s.batches)], ["类型数", fmt(o.types)], ["字段种类", fmt(o.fields)], ["类型树修订", fmt(s.tree_rev)],
    ["对象 ID 冲突", fmt(s.id_conflicts)], ["存储占用", bytes(o.db_bytes)], ["落盘级别", w0.sync],
  ];
  $("#kpis").innerHTML =
    `<div class="kpis-main">${primary.map(([l, v, c]) => `<div class="kpi big ${c || ""}"><div class="v">${v}</div><div class="l">${l}</div></div>`).join("")}</div>` +
    `<div class="kpis-sub">${secondary.map(([l, v]) => `<div class="kpi small"><div class="v">${esc(v)}</div><div class="l">${l}</div></div>`).join("")}` +
    `<div class="kpi small link"><div class="l">完整性对账（逐批核对 receipt）见「数据可信」页</div></div></div>`;
  const sec = o.rate.map((r) => r.sec), now = Math.floor(Date.now() / 1000);
  const map = new Map(o.rate.map((r) => [r.sec, r.n]));
  const xs = [], ys = [];
  const start = sec.length ? Math.max(sec[0], now - 180) : now - 60;
  for (let t = start; t <= now; t++) { xs.push(new Date(t * 1000).toTimeString().slice(0, 8)); ys.push(map.get(t) || 0); }
  chart("rateChart").setOption({
    backgroundColor: "transparent", grid: { left: 50, right: 10, top: 10, bottom: 30 }, tooltip: { trigger: "axis" },
    xAxis: { type: "category", data: xs }, yAxis: { type: "value" },
    series: [{ type: "line", data: ys, showSymbol: false, areaStyle: { opacity: 0.2 }, lineStyle: { width: 1.5 }, color: "#6ea8fe" }],
  });
  chart("catChart").setOption({
    backgroundColor: "transparent", grid: { left: 90, right: 20, top: 10, bottom: 20 }, tooltip: {},
    xAxis: { type: "value" }, yAxis: { type: "category", inverse: true, data: o.categories.map((c) => c.name) },
    series: [{ type: "bar", data: o.categories.map((c) => ({ value: c.n, itemStyle: { color: catColor(c.id) } })) }],
  });
  const w = o.writer;
  $("#writer").innerHTML = [
    ["写入队列深度", w.queue], ["组提交次数", fmt(w.groups)], ["已处理消息", fmt(w.messages)],
    ["平均每组消息", w.groups ? (w.messages / w.groups).toFixed(1) : "–"], ["最近一次提交", w.last_commit_ms + " ms"],
    ["最长一次提交", w.max_commit_ms + " ms"], ["确认延迟 p50 / p99", `${w.ack_p50_ms ?? "–"} / ${w.ack_p99_ms ?? "–"} ms`],
    ["HTTP 结果", Object.entries(w.http).map(([k, v]) => `${k.slice(5)}: ${fmt(v)}`).join(" · ") || "–"],
    ["持久化策略", `SQLite WAL · synchronous=${w.sync} · 先提交后确认`], ["接收字节", bytes(s.bytes || 0)], ["服务运行", o.uptime_s + " s"],
  ].map(([k, v]) => `<div>${k}</div><div>${esc(v)}</div>`).join("");
  const rj = await api("/api/rejects?" + qs({ test: S.test, limit: 8 }));
  $("#rejectsMini").innerHTML = rejectTable(rj, true);
  $("#quality").innerHTML = [
    ["已落盘对象", fmt(s.objects)], ["已落盘批次", fmt(s.batches)], ["吸收的重复批次", fmt(s.dup_batches)],
    ["吸收的重复对象", fmt(s.dup_objects)], ["重复的树修订", fmt(s.dup_tree)], ["拒收的故障消息", fmt(s.rejected)],
    ["对象 ID 冲突", fmt(s.id_conflicts)], ["首次 / 最近写入", `${ts(s.first_ms)} / ${ts(s.last_ms)}`],
  ].map(([k, v]) => `<div>${k}</div><div>${esc(v)}</div>`).join("");
}

function rejectTable(rows, mini) {
  if (!rows.length) return `<p class="hint">暂无拒收消息。</p>`;
  return `<table><thead><tr><th>时间</th><th>类型</th><th>批次/修订</th><th>码</th><th>原因</th>${mini ? "" : "<th>原文片段</th>"}</tr></thead><tbody>` +
    rows.map((r) => `<tr><td>${ts(r.received_ms)}</td><td>${esc(r.kind)}</td><td>${r.batch_id ?? r.revision ?? "–"}</td><td>${r.code}</td>` +
      `<td class="wrap">${esc(r.reason)}</td>${mini ? "" : `<td class="wrap"><code>${esc(r.sample?.slice(0, 260))}</code></td>`}</tr>`).join("") + "</tbody></table>";
}

// ---------------------------------------------------------------- explore
function renderTreeNode(id, depth, filter) {
  const t = S.tree, n = t.by.get(id);
  if (!n) return "";
  const kids = (t.kids.get(id) || []).slice().sort((a, b) => t.total.get(b) - t.total.get(a));
  const open = depth < 1 || filter;
  const match = !filter || n.name.toLowerCase().includes(filter) || kids.some((k) => subtreeMatches(k, filter));
  if (!match) return "";
  const isNew = n.created_rev && t.maxRev && n.created_rev >= t.maxRev - 2 && n.created_rev > 1;
  return `<li data-id="${id}"><div class="node ${S.sel === id ? "sel" : ""}" data-id="${id}">
    <span class="tg">${kids.length ? (open ? "▾" : "▸") : "·"}</span>
    <span class="nm" style="border-left:3px solid ${typeColor(id)};padding-left:5px">${esc(n.name)}</span>
    ${isNew ? '<span class="new">新</span>' : ""}<span class="ct">${fmt(t.total.get(id))}</span></div>
    ${kids.length ? `<ul ${open ? "" : 'style="display:none"'}>${open ? kids.map((k) => renderTreeNode(k, depth + 1, filter)).join("") : ""}</ul>` : ""}</li>`;
}
function subtreeMatches(id, f) {
  const t = S.tree, n = t.by.get(id);
  return n && (n.name.toLowerCase().includes(f) || (t.kids.get(id) || []).some((k) => subtreeMatches(k, f)));
}
async function renderTree(force) {
  await loadTree(force);
  const f = $("#treeFilter").value.trim().toLowerCase();
  $("#tree").innerHTML = `<ul>${S.tree.roots.map((r) => renderTreeNode(r, 0, f)).join("")}</ul>`;
  $("#treeInfo").textContent = `${fmt(S.tree.by.size)} 个类型`;
}
$("#tree").onclick = (e) => {
  const node = e.target.closest(".node");
  if (!node) return;
  const id = Number(node.dataset.id);
  const li = node.parentElement, ul = li.querySelector(":scope > ul");
  if (e.target.classList.contains("tg") && ul) {
    if (!ul.innerHTML) ul.innerHTML = (S.tree.kids.get(id) || []).slice().sort((a, b) => S.tree.total.get(b) - S.tree.total.get(a)).map((k) => renderTreeNode(k, 9, "")).join("");
    const hidden = ul.style.display === "none";
    ul.style.display = hidden ? "" : "none";
    e.target.textContent = hidden ? "▾" : "▸";
    return;
  }
  S.sel = S.sel === id ? null : id;
  document.querySelectorAll("#tree .node.sel").forEach((x) => x.classList.remove("sel"));
  if (S.sel) node.classList.add("sel");
  loadObjects(true);
};
$("#treeFilter").oninput = () => renderTree(false);

async function loadObjects(reset) {
  if (reset) { S.next = null; $("#objTable tbody").innerHTML = ""; }
  const q = $("#q").value.trim();
  const d = await api("/api/objects?" + qs({ test: S.test, type: S.sel, q, before: reset ? null : S.next, limit: 100 }));
  S.next = d.next;
  const path = S.sel ? pathOf(S.sel).map((x) => x.name).join(" › ") : "全部对象";
  $("#crumb").textContent = path + (q ? ` · “${q}”` : "");
  $("#objTable tbody").insertAdjacentHTML("beforeend", d.items.map((o) => `<tr data-id="${esc(o.id)}">
    <td>${esc(o.id)}</td><td>${esc(o.name)}</td><td><span style="color:${typeColor(o.type_id)}">●</span> ${esc(o.type_name)}</td>
    <td>${ts(o.ts_ms)}</td><td>${esc(o.frame_kind ?? "—")}</td>
    <td>${o.x == null ? "—" : `${fmt(o.x)}, ${fmt(o.y)}, ${fmt(o.z)}`}</td></tr>`).join(""));
  if (reset && !d.items.length) $("#objTable tbody").innerHTML = `<tr><td colspan="6" class="hint">没有匹配的对象</td></tr>`;
  $("#more").style.display = d.next == null ? "none" : "";
}
function pathOf(id) {
  const out = []; let t = id, g = 0;
  while (t && S.tree?.by.has(t) && g++ < 64) { const n = S.tree.by.get(t); out.push(n); t = n.parent; }
  return out.reverse();
}
$("#objTable").onclick = (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) showObject(tr.dataset.id); };
$("#more").onclick = () => loadObjects(false);
$("#qGo").onclick = () => loadObjects(true);
$("#q").onkeydown = (e) => { if (e.key === "Enter") loadObjects(true); };

function objectHtml(o) {
  const p = o.position;
  return `<h2 style="margin:0 0 4px">${esc(o.name)}</h2>
    <div class="hint">${esc(o.id)} · 序号 ${o.sequence} · 批次 ${o.batch_id}</div>
    <p>${o.type_path.map((t) => `<span class="badge">${esc(t.name)}</span>`).join(" › ")}</p>
    <div class="kv"><div>观测时间</div><div>${ts(o.timestamp_ms)} (${o.timestamp_ms})</div>
    <div>位置</div><div>${p ? `${esc(p.frame_kind)} · ${esc(p.frame_id)} · (${p.x}, ${p.y}, ${p.z}) ${esc(p.unit)}` : "无（非空间对象）"}</div>
    <div>属性数</div><div>${Object.keys(o.attributes || {}).length}</div></div>
    <h3>属性</h3><pre>${esc(JSON.stringify(o.attributes, null, 2))}</pre>`;
}
async function showObject(id) {
  const o = await api("/api/object/" + encodeURIComponent(id));
  $("#mbody").innerHTML = objectHtml(o);
  $("#modal").classList.remove("hidden");
}
$("#mclose").onclick = () => $("#modal").classList.add("hidden");
$("#modal").onclick = (e) => { if (e.target.id === "modal") $("#modal").classList.add("hidden"); };

// ---------------------------------------------------------------- map (immersive observation window)
// Real coordinates only: decorative star dust is fixed to the screen (it never pans/zooms and is not
// clickable), data stars move with the view. projections index into points [seq, type, x, y, z].
const AX = { xy: [2, 3, "X", "Y"], xz: [2, 4, "X", "Z"], yz: [3, 4, "Y", "Z"] };
const M = { pts: [], frames: [], frame: null, window: 0, proj: "xy", view: null, key: "", hidden: new Set(),
            sel: null, hover: null, drag: null, updated: null, loading: false, toScreen: null, grid: true,
            base: document.createElement("canvas"), raf: 0 };
const MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
const frameKey = (f) => (f ? `${f.frame_kind}|${f.frame_id}|${f.unit}` : "");
const mapKey = () => `${frameKey(M.frame)}|${M.proj}|${S.sel ?? ""}`;
const fmtAxis = (v) => (v !== 0 && (Math.abs(v) >= 1e7 || Math.abs(v) < 1e-2) ? v.toExponential(1) : fmt(Math.round(v * 100) / 100));
function niceStep(raw) {
  const e = Math.pow(10, Math.floor(Math.log10(raw))), m = raw / e;
  return (m < 1.5 ? 1 : m < 3.5 ? 2 : m < 7.5 ? 5 : 10) * e;
}

async function renderMap(keepView) {
  if (M.loading) return;
  M.loading = true;
  try {
    await loadTree(false);
    const cur = M.frame || {};
    const d = await api("/api/map?" + qs({ test: S.test, kind: cur.frame_kind, frame_id: cur.frame_id, unit: cur.unit, type: S.sel, limit: 20000 }));
    M.frames = d.frames;
    M.frame = d.frame;
    M.window = d.window || 0;
    M.pts = d.points;
    M.updated = new Date();
    const k = mapKey();
    if (!keepView || k !== M.key) { M.view = null; M.sel = null; $("#mapDetailWrap").classList.add("hidden"); }
    M.key = k;
    $("#frame").innerHTML = d.frames.map((f, i) => `<option value="${i}" ${frameKey(f) === frameKey(d.frame) ? "selected" : ""}>${esc(f.frame_kind)} · ${esc(f.frame_id)} · ${esc(f.unit)}（累计 ${fmt(f.n)}）</option>`).join("");
    renderMapChrome();
    drawMap();
  } finally { M.loading = false; }
}

function renderMapChrome() {
  const f = M.frame;
  const chip = $("#mapFilter");
  if (S.sel && S.tree?.by.has(S.sel)) {
    chip.innerHTML = `当前类型：${esc(pathOf(S.sel).map((x) => x.name).join(" › "))} <button id="mapClear" title="清除类型筛选">✕ 清除</button>`;
    chip.classList.remove("hidden");
    $("#mapClear").onclick = () => { S.sel = null; document.querySelectorAll("#tree .node.sel").forEach((x) => x.classList.remove("sel")); renderMap(false); };
  } else chip.classList.add("hidden");
  const total = f ? (M.frames.find((x) => frameKey(x) === frameKey(f))?.n ?? 0) : 0;
  const shown = M.pts.filter((p) => !M.hidden.has(topId(p[1]))).length;
  $("#mapCount").textContent = f
    ? `${f.frame_kind} · ${f.frame_id} · ${f.unit}　本次显示 ${fmt(shown)} 点 · 取样 ${fmt(M.pts.length)}（最新 ${fmt(M.window)} 个对象序号窗口内最多 20,000 点）· 累计 ${fmt(total)} 个对象`
    : "这一轮没有带坐标的对象";
  $("#mapUpdated").textContent = M.updated ? `最后更新 ${M.updated.toTimeString().slice(0, 8)}` : "";
  const cnt = new Map();
  for (const p of M.pts) { const t = topId(p[1]); cnt.set(t, (cnt.get(t) || 0) + 1); }
  const tops = [...cnt.keys()].sort((a, b) => cnt.get(b) - cnt.get(a));
  $("#legend").innerHTML = tops.map((t) => `<button class="lg ${M.hidden.has(t) ? "off" : ""}" data-top="${t}" title="点击显示/隐藏">
      <i style="background:${catColor(t)};box-shadow:0 0 6px ${catColor(t)}"></i><b>${esc(S.tree?.by.get(t)?.name ?? "#" + t)}</b><span>${fmt(cnt.get(t))}</span></button>`).join("")
    || `<p class="hint">无</p>`;
}

function fitView(W, H) {
  const [ix, iy] = AX[M.proj];
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity, n = 0;
  for (const p of M.pts) {
    if (M.hidden.has(topId(p[1]))) continue;
    const x = p[ix], y = p[iy]; n++;
    if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y;
  }
  if (!n) return { cx: 0, cy: 0, k: 1 };
  // leave room for the floating panels on the left and right
  const padX = Math.min(300, W * 0.2), padY = 60, dx = (x1 - x0) || 1, dy = (y1 - y0) || 1;
  return { cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, k: Math.max(1e-12, Math.min((W - 2 * padX) / dx, (H - 2 * padY) / dy)) };
}

// decorative deep-space background, cached per canvas size (deterministic, dim, screen-fixed)
const SKYBG = { w: 0, h: 0, d: 0, c: null };
function skyBackground(W, H, dpr) {
  if (SKYBG.c && SKYBG.w === W && SKYBG.h === H && SKYBG.d === dpr) return SKYBG.c;
  const c = document.createElement("canvas");
  c.width = Math.round(W * dpr); c.height = Math.round(H * dpr);
  const g = c.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0);
  const base = g.createLinearGradient(0, 0, 0, H);
  base.addColorStop(0, "#02040a"); base.addColorStop(1, "#050915");
  g.fillStyle = base; g.fillRect(0, 0, W, H);
  const R = Math.max(W, H);
  const neb = (x, y, r, col) => { const gr = g.createRadialGradient(x, y, 0, x, y, r); gr.addColorStop(0, col); gr.addColorStop(1, "rgba(0,0,0,0)"); g.fillStyle = gr; g.fillRect(0, 0, W, H); };
  neb(W * 0.22, H * 0.28, R * 0.45, "rgba(78,64,150,0.10)");
  neb(W * 0.80, H * 0.74, R * 0.42, "rgba(36,96,160,0.08)");
  neb(W * 0.58, H * 0.10, R * 0.30, "rgba(120,60,130,0.05)");
  let seed = 20261101;
  const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const n = Math.round((W * H) / 2400);
  for (let i = 0; i < n; i++) {
    const x = rnd() * W, y = rnd() * H, a = 0.04 + rnd() * 0.16, s = rnd() < 0.93 ? 0.7 : 1.2;
    g.fillStyle = `rgba(205,214,240,${a.toFixed(3)})`; g.fillRect(x, y, s, s);
  }
  Object.assign(SKYBG, { w: W, h: H, d: dpr, c });
  return c;
}

// base layer: background, grid, data stars, axes (redrawn only when data or view change)
function drawMap() {
  const cv = $("#sky"), dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = cv.clientHeight;
  if (!W || !H) return;
  for (const c of [cv, M.base]) {
    if (c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); }
  }
  const g = M.base.getContext("2d");
  g.setTransform(1, 0, 0, 1, 0, 0);
  g.drawImage(skyBackground(W, H, dpr), 0, 0);
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  const f = M.frame;
  if (!f || !M.pts.length) {
    M.toScreen = null; g.fillStyle = "#8a97b6"; g.font = "14px sans-serif"; g.fillText("该参考系暂无对象", W / 2 - 60, H / 2);
    composite(); return;
  }
  if (!M.view) M.view = fitView(W, H);
  const { cx, cy, k } = M.view, [ix, iy, ax, ay] = AX[M.proj];
  const sx = (x) => W / 2 + (x - cx) * k, sy = (y) => H / 2 - (y - cy) * k;
  M.toScreen = (p) => [sx(p[ix]), sy(p[iy])];
  const step = niceStep(110 / k);
  const xa = cx - W / 2 / k, xb = cx + W / 2 / k, ya = cy - H / 2 / k, yb = cy + H / 2 / k;
  if (M.grid) {
    g.lineWidth = 1; g.strokeStyle = "rgba(120,190,255,0.06)"; g.beginPath();
    for (let x = Math.ceil(xa / step) * step; x <= xb; x += step) { const X = Math.round(sx(x)) + 0.5; g.moveTo(X, 0); g.lineTo(X, H); }
    for (let y = Math.ceil(ya / step) * step; y <= yb; y += step) { const Y = Math.round(sy(y)) + 0.5; g.moveTo(0, Y); g.lineTo(W, Y); }
    g.stroke();
    g.font = `10px ${MONO}`; g.fillStyle = "rgba(140,170,210,0.45)";
    for (let x = Math.ceil(xa / step) * step; x <= xb; x += step) g.fillText(fmtAxis(x), sx(x) + 3, H - 30);
    for (let y = Math.ceil(ya / step) * step; y <= yb; y += step) g.fillText(fmtAxis(y), W / 2 > 300 ? 270 : 6, sy(y) - 3);
  }
  if (xa <= 0 && 0 <= xb && ya <= 0 && 0 <= yb) {
    const X = sx(0), Y = sy(0);
    g.strokeStyle = "rgba(245,184,75,0.45)"; g.lineWidth = 1; g.beginPath(); g.moveTo(X - 10, Y); g.lineTo(X + 10, Y); g.moveTo(X, Y - 10); g.lineTo(X, Y + 10); g.stroke();
    g.fillStyle = "rgba(245,184,75,0.75)"; g.font = `10px ${MONO}`; g.fillText("原点 (0,0)", X + 6, Y - 6);
  }
  // data stars: soft halo + bright core, per colour (source-over keeps dense regions coloured, not white)
  const groups = new Map();
  for (const p of M.pts) {
    const t = topId(p[1]);
    if (M.hidden.has(t)) continue;
    const X = sx(p[ix]), Y = sy(p[iy]);
    if (X < -6 || Y < -6 || X > W + 6 || Y > H + 6) continue;
    const c = catColor(t);
    let a = groups.get(c); if (!a) groups.set(c, (a = []));
    a.push(X, Y);
  }
  const pass = (rad, alpha) => {
    g.globalAlpha = alpha;
    for (const [c, a] of groups) {
      g.fillStyle = c; g.beginPath();
      for (let i = 0; i < a.length; i += 2) { g.moveTo(a[i] + rad, a[i + 1]); g.arc(a[i], a[i + 1], rad, 0, 6.2832); }
      g.fill();
    }
  };
  pass(3.6, 0.09);   // halo
  pass(1.35, 0.92);  // core
  g.globalAlpha = 0.85; g.fillStyle = "#ffffff";   // pin-point highlight in each core
  for (const a of groups.values()) for (let i = 0; i < a.length; i += 2) g.fillRect(a[i] - 0.35, a[i + 1] - 0.35, 0.7, 0.7);
  g.globalAlpha = 1;
  // axis names and scale bar (bottom centre)
  g.fillStyle = "rgba(160,190,225,0.8)"; g.font = `11px ${MONO}`;
  g.fillText(`${ax} →`, W - 380 > W / 2 ? W - 380 : W - 60, H - 14); g.fillText(`↑ ${ay}`, W / 2, 16);
  const bar = step * k, bx = W / 2 - bar / 2, by = H - 16;
  g.strokeStyle = "rgba(200,225,255,0.75)"; g.lineWidth = 1.5; g.beginPath();
  g.moveTo(bx, by); g.lineTo(bx + bar, by); g.moveTo(bx, by - 4); g.lineTo(bx, by + 4); g.moveTo(bx + bar, by - 4); g.lineTo(bx + bar, by + 4); g.stroke();
  g.fillText(`${fmtAxis(step)} ${f.unit}`, bx + bar + 8, by + 4);
  composite();
}

// top layer: base + locator ring (hover) + breathing target lock (selection); cheap enough per frame
function composite(t) {
  const cv = $("#sky"), dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight;
  if (!W || !H) return;
  const g = cv.getContext("2d");
  g.setTransform(1, 0, 0, 1, 0, 0);
  g.drawImage(M.base, 0, 0);
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  if (!M.toScreen) return;
  if (M.hover && M.hover !== M.sel) {
    const [X, Y] = M.toScreen(M.hover);
    g.strokeStyle = "rgba(110,220,255,0.9)"; g.lineWidth = 1.2; g.beginPath(); g.arc(X, Y, 7, 0, 6.2832);
    for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) { g.moveTo(X + dx * 9, Y + dy * 9); g.lineTo(X + dx * 14, Y + dy * 14); }
    g.stroke();
  }
  if (M.sel) {
    const [X, Y] = M.toScreen(M.sel), ph = ((t ?? performance.now()) / 650) % 6.2832;
    const r = 10 + 2.5 * Math.sin(ph), a = 0.55 + 0.35 * Math.sin(ph);
    g.save(); g.shadowColor = "#f5b84b"; g.shadowBlur = 16;
    g.strokeStyle = `rgba(245,184,75,${a.toFixed(2)})`; g.lineWidth = 1.6; g.beginPath(); g.arc(X, Y, r, 0, 6.2832); g.stroke();
    g.restore();
    g.strokeStyle = "rgba(245,184,75,0.9)"; g.lineWidth = 1.4; g.beginPath();   // corner brackets
    const s = 16, l = 5;
    for (const [sx2, sy2] of [[-1, -1], [1, -1], [-1, 1], [1, 1]]) {
      g.moveTo(X + sx2 * s, Y + sy2 * (s - l)); g.lineTo(X + sx2 * s, Y + sy2 * s); g.lineTo(X + sx2 * (s - l), Y + sy2 * s);
    }
    g.stroke();
  }
}
function pulse() {
  M.raf = 0;
  if (S.tab !== "map" || !M.sel || document.hidden) return;
  composite(performance.now());
  M.raf = requestAnimationFrame(pulse);
}
const startPulse = () => { if (!M.raf) M.raf = requestAnimationFrame(pulse); };

function pickAt(mx, my, rad = 9) {
  if (!M.toScreen) return null;
  let best = null, bd = rad * rad;
  for (const p of M.pts) {
    if (M.hidden.has(topId(p[1]))) continue;
    const [X, Y] = M.toScreen(p), d = (X - mx) ** 2 + (Y - my) ** 2;
    if (d < bd) { bd = d; best = p; }
  }
  return best;
}
async function showMapDetail(id) {
  const wrap = $("#mapDetailWrap");
  wrap.classList.remove("hidden");
  $("#mapDetail").innerHTML = `<p class="hint">读取 ${esc(id)} …</p>`;
  try { $("#mapDetail").innerHTML = objectHtml(await api("/api/object/" + encodeURIComponent(id))); }
  catch (e) { $("#mapDetail").innerHTML = `<p class="badge bad">${esc(e.message)}</p>`; }
}
const sky = $("#sky");
const rel = (e) => { const r = sky.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
sky.addEventListener("wheel", (e) => {
  e.preventDefault();
  if (!M.view) return;
  const [mx, my] = rel(e), W = sky.clientWidth, H = sky.clientHeight, v = M.view;
  const dx = v.cx + (mx - W / 2) / v.k, dy = v.cy - (my - H / 2) / v.k;          // the data point under the cursor stays put
  const k2 = Math.min(Math.max(v.k * Math.exp(-e.deltaY * 0.0015), 1e-12), 1e12);
  M.view = { k: k2, cx: dx - (mx - W / 2) / k2, cy: dy + (my - H / 2) / k2 };
  drawMap();
}, { passive: false });
sky.addEventListener("mousedown", (e) => { const [mx, my] = rel(e); M.drag = { mx, my, cx: M.view?.cx, cy: M.view?.cy, moved: false }; });
window.addEventListener("mouseup", (e) => {
  const d = M.drag; M.drag = null;
  if (!d || d.moved || S.tab !== "map") return;
  const [mx, my] = rel(e), p = pickAt(mx, my);
  if (p) { M.sel = p; composite(); startPulse(); showMapDetail(`${S.test}-${p[0]}`); }
});
sky.addEventListener("mousemove", (e) => {
  const [mx, my] = rel(e), d = M.drag;
  if (d && M.view) {
    if (Math.abs(mx - d.mx) + Math.abs(my - d.my) > 3) d.moved = true;
    if (d.moved) { M.view = { ...M.view, cx: d.cx - (mx - d.mx) / M.view.k, cy: d.cy + (my - d.my) / M.view.k }; drawMap(); }
    $("#tip").classList.add("hidden");
    return;
  }
  const p = pickAt(mx, my);
  if (p !== M.hover) { M.hover = p; composite(); }
  const tip = $("#tip");
  if (p) {
    const [ix, iy] = AX[M.proj], n = S.tree?.by.get(p[1]);
    tip.textContent = `${n ? n.name : "#" + p[1]}  ·  ${S.test}-${p[0]}  ·  (${fmtAxis(p[ix])}, ${fmtAxis(p[iy])}) ${M.frame?.unit ?? ""}`;
    tip.style.left = Math.max(0, Math.min(mx + 16, sky.clientWidth - 330)) + "px"; tip.style.top = my + 14 + "px";
    tip.classList.remove("hidden");
  } else tip.classList.add("hidden");
});
sky.addEventListener("mouseleave", () => { $("#tip").classList.add("hidden"); if (M.hover) { M.hover = null; composite(); } });
$("#frame").onchange = () => { M.frame = M.frames[Number($("#frame").value)]; renderMap(false); };
document.querySelectorAll("#projSeg button").forEach((b) => b.onclick = () => {
  M.proj = b.dataset.proj;
  document.querySelectorAll("#projSeg button").forEach((x) => x.classList.toggle("on", x === b));
  M.view = null; M.key = mapKey(); drawMap();
});
$("#fit").onclick = () => { M.view = null; drawMap(); };
$("#resetView").onclick = () => { M.view = null; M.hidden.clear(); M.sel = null; $("#mapDetailWrap").classList.add("hidden"); renderMapChrome(); drawMap(); };
$("#mapGrid").onchange = () => { M.grid = $("#mapGrid").checked; drawMap(); };
$("#mdClose").onclick = () => { M.sel = null; $("#mapDetailWrap").classList.add("hidden"); composite(); };
$("#legend").onclick = (e) => {
  const b = e.target.closest("button[data-top]"); if (!b) return;
  const t = Number(b.dataset.top);
  M.hidden.has(t) ? M.hidden.delete(t) : M.hidden.add(t);
  renderMapChrome(); drawMap();
};

// ---------------------------------------------------------------- evolution
async function renderEvolve() {
  await loadTree(false);
  const [ch, fl] = await Promise.all([api("/api/changes?" + qs({ test: S.test })), api("/api/fields?" + qs({ test: S.test, type: S.sel, limit: 300 }))]);
  const revs = ch.revisions.slice().reverse();
  chart("revChart").setOption({
    backgroundColor: "transparent", grid: { left: 40, right: 10, top: 10, bottom: 30 }, tooltip: { trigger: "axis" },
    xAxis: { type: "category", data: revs.map((r) => r.revision) }, yAxis: { type: "value" },
    series: [{ type: "bar", data: revs.map((r) => r.n_changes), color: "#c084fc" }],
  });
  const opName = { rename: "重命名", move: "移动", update: "更新", delete: "删除" };
  const nm = (id) => S.tree.by.get(id)?.name ?? (id === 0 ? "根" : `#${id}`);
  $("#changes").innerHTML = ch.changes.length ? `<table><thead><tr><th>修订</th><th>操作</th><th>类型</th><th>变化</th></tr></thead><tbody>` +
    ch.changes.map((c) => `<tr><td>${c.revision}</td><td>${opName[c.op] || c.op}</td><td>#${c.type_id}</td><td class="wrap">${
      c.op === "move" ? `${esc(nm(c.old_parent))} → ${esc(nm(c.parent_id))}` : `${esc(c.old_name)} → ${esc(c.name)}`}</td></tr>`).join("") + "</tbody></table>"
    : `<p class="hint">尚无重命名或移动。</p>`;
  $("#fields").innerHTML = fl.length ? `<table><thead><tr><th>字段</th><th>值类型</th><th>所属类型</th><th>首次出现（对象序号 / 批次）</th><th>时间</th></tr></thead><tbody>` +
    fl.map((f) => `<tr><td><code>${esc(f.name)}</code></td><td>${f.json_type}</td><td>${esc(f.type_name)}</td><td>${f.first_seq} / ${f.first_batch}</td><td>${ts(f.first_ms)}</td></tr>`).join("") + "</tbody></table>"
    : `<p class="hint">暂无字段。</p>`;
}

// ---------------------------------------------------------------- trust
async function renderTrust() {
  await renderOverview();
  const rj = await api("/api/rejects?" + qs({ test: S.test, limit: 100 }));
  $("#rejects").innerHTML = rejectTable(rj, false);
}
$("#receipt").onchange = async (e) => {
  const f = e.target.files[0]; if (!f) return;
  $("#recon").innerHTML = `<p class="hint">对账中…</p>`;
  try {
    const r = await api("/api/reconcile", { method: "POST", headers: { "Content-Type": "application/json" }, body: await f.text() });
    const row = (k, v, good) => `<div class="recon-row"><span>${k}</span><span class="badge ${good === undefined ? "" : good ? "ok" : "bad"}">${esc(v)}</span></div>`;
    const b = r.batches, o = r.objects, fa = r.faults, du = r.duplicates, t = r.tree;
    $("#recon").innerHTML = `<h3>${esc(r.test_id)} ` +
      (r.integrity_ok ? '<span class="badge ok">数据完整：确认的都在、错的没进</span>' : '<span class="badge bad">数据不一致</span>') + " " +
      (r.exercise_complete ? '<span class="badge ok">演练完整</span>' : `<span class="badge">演练未完整：${esc(r.run.state)} / ${esc(r.run.finish_reason)}</span>`) + `</h3>` +
      row("发生器已发送批次", fmt(b.sent)) + row("我们确认（2xx）的批次", fmt(b.acked_by_us)) + row("实际落盘批次", fmt(b.stored)) +
      row("已确认但未落盘（丢失）", fmt(b.acked_but_missing), b.acked_but_missing === 0) +
      row("落盘但从未发送（幻影）", fmt(b.stored_never_sent), b.stored_never_sent === 0) +
      row("落盘但确认未送达（可接受）", fmt(b.stored_without_ack)) +
      row("对象：发生器确认 / 已确认批次中落盘", `${fmt(o.generator_acknowledged)} / ${fmt(o.stored_in_acked_batches)}`, o.generator_acknowledged == null || o.generator_acknowledged === o.stored_in_acked_batches) +
      row("对象：库中行数 / 落盘批次合计", `${fmt(o.rows_in_store)} / ${fmt(o.sum_of_stored_batches)}`, o.rows_in_store + o.identical_resends_kept_once === o.sum_of_stored_batches) +
      row("故障消息：发送 / 拒收 / 误收", `${fa.sent} / ${fa.rejected} / ${fa.acked}`, fa.acked === 0 && fa.stored === 0) +
      row("重复批次：发送 / 吸收", `${du.sent} / ${du.absorbed_by_store}`) +
      row("故障集合：发送但未拒收 / 拒收但未发送", `${fa.sent_but_not_rejected_ids.length} / ${fa.rejected_but_not_sent_ids.length}`, fa.sent_but_not_rejected_ids.length === 0 && fa.rejected_but_not_sent_ids.length === 0) +
      row("类型树修订：发生器确认 / 已存最大 / 缺口", `${t.generator_acknowledged} / ${t.stored_max} / ${t.gaps_in_acked_range}`, t.gaps_in_acked_range === 0);
  } catch (err) { $("#recon").innerHTML = `<p class="badge bad">无法对账：${esc(err.message)}</p>`; }
};

// ---------------------------------------------------------------- refresh loop
let busy = false;
async function refresh(force) {
  if (busy) return; busy = true;
  try {
    if (!S.test || force) await loadTests();
    if (!S.test) { $("#kpis").innerHTML = `<div class="kpi"><div class="v">等待数据</div><div class="l">发生器尚未发送任何消息</div></div>`; return; }
    if (S.tab === "overview") await renderOverview();
    else if (S.tab === "explore") { await renderTree(force); if (force) await loadObjects(true); }
    else if (S.tab === "map") { if (force) await renderMap(true); }
    else if (S.tab === "evolve") await renderEvolve();
    else if (S.tab === "trust") await renderTrust();
    $("#live").classList.remove("off");
  } catch (e) { console.warn(e); $("#live").classList.add("off"); }
  finally { busy = false; }
}
let tick = 0;
setInterval(() => {
  tick++;
  if (S.tab === "overview" || (S.tab === "trust" && tick % 3 === 0)) refresh(false);
  if (S.tab === "map" && $("#mapAuto").checked && tick % 3 === 0) renderMap(true).catch((e) => console.warn(e));
  if (tick % 10 === 0) { loadTests(); S.treeStale = true; }
}, 2000);
refresh(true);
