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
$("#test").onchange = () => { S.test = $("#test").value; S.tree = null; S.sel = null; refresh(true); };

// ---------------------------------------------------------------- tree helpers
async function loadTree(force) {
  if (S.tree && !force) return S.tree;
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
  const color = new Map(tops.map((t, i) => [t, PALETTE[i % PALETTE.length]]));
  S.tree = { by, kids, total, roots, tops, topOf, color, maxRev: Math.max(0, ...d.nodes.map((n) => n.created_rev || 0)) };
  return S.tree;
}
const typeColor = (tid) => S.tree?.color.get(S.tree.topOf.get(tid)) || "#8a97b6";

// ---------------------------------------------------------------- overview
async function renderOverview() {
  const o = await api("/api/overview?" + qs({ test: S.test }));
  const s = o.stats;
  const last = o.rate.length ? o.rate[o.rate.length - 1] : null;
  const recent = o.rate.filter((r) => r.sec >= Date.now() / 1000 - 6);
  const nowRate = recent.length ? Math.round(recent.reduce((a, r) => a + r.n, 0) / Math.max(1, recent.length)) : 0;
  const tiles = [
    ["已落盘对象", fmt(s.objects), "good"], ["当前摄入 /s", fmt(nowRate)], ["已确认批次", fmt(s.batches)],
    ["类型数", fmt(o.types)], ["字段种类", fmt(o.fields)], ["类型树修订", fmt(s.tree_rev)],
    ["吸收的重复批", fmt(s.dup_batches), s.dup_batches ? "warn" : ""], ["拒收的故障消息", fmt(s.rejected), s.rejected ? "warn" : ""],
    ["确认延迟 p99", o.writer.ack_p99_ms == null ? "–" : o.writer.ack_p99_ms + " ms"], ["存储占用", bytes(o.db_bytes)],
  ];
  $("#kpis").innerHTML = tiles.map(([l, v, c]) => `<div class="kpi ${c || ""}"><div class="v">${v}</div><div class="l">${l}</div></div>`).join("");
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
    series: [{ type: "bar", data: o.categories.map((c, i) => ({ value: c.n, itemStyle: { color: PALETTE[i % PALETTE.length] } })) }],
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

async function showObject(id) {
  const o = await api("/api/object/" + encodeURIComponent(id));
  const p = o.position;
  $("#mbody").innerHTML = `<h2 style="margin:0 0 4px">${esc(o.name)}</h2>
    <div class="hint">${esc(o.id)} · 序号 ${o.sequence} · 批次 ${o.batch_id}</div>
    <p>${o.type_path.map((t) => `<span class="badge">${esc(t.name)}</span>`).join(" › ")}</p>
    <div class="kv"><div>观测时间</div><div>${ts(o.timestamp_ms)} (${o.timestamp_ms})</div>
    <div>位置</div><div>${p ? `${esc(p.frame_kind)} · ${esc(p.frame_id)} · (${p.x}, ${p.y}, ${p.z}) ${esc(p.unit)}` : "无（非空间对象）"}</div>
    <div>属性数</div><div>${Object.keys(o.attributes).length}</div></div>
    <h3>属性</h3><pre>${esc(JSON.stringify(o.attributes, null, 2))}</pre>`;
  $("#modal").classList.remove("hidden");
}
$("#mclose").onclick = () => $("#modal").classList.add("hidden");
$("#modal").onclick = (e) => { if (e.target.id === "modal") $("#modal").classList.add("hidden"); };

// ---------------------------------------------------------------- map
async function renderMap() {
  await loadTree(false);
  const frame = $("#frame").value || "galactic";
  const d = await api("/api/map?" + qs({ test: S.test, frame, type: S.sel, limit: 20000 }));
  const fs = $("#frame");
  if (fs.options.length !== d.frames.length) {
    fs.innerHTML = d.frames.map((f) => `<option ${f === frame ? "selected" : ""}>${esc(f)}</option>`).join("");
  }
  S.mapPts = d.points;
  drawMap();
  $("#legend").innerHTML = S.tree.tops.map((t) => `<span><i style="background:${S.tree.color.get(t)}"></i>${esc(S.tree.by.get(t).name)}</span>`).join("");
}
function drawMap() {
  const cv = $("#sky"), dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  cv.width = w * dpr; cv.height = h * dpr;
  const g = cv.getContext("2d"); g.scale(dpr, dpr);
  g.fillStyle = "#050914"; g.fillRect(0, 0, w, h);
  const pts = S.mapPts; if (!pts.length) { g.fillStyle = "#8a97b6"; g.fillText("该坐标系暂无对象", 20, 30); return; }
  const yi = $("#proj").value === "xz" ? 4 : 3;
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const p of pts) { x0 = Math.min(x0, p[2]); x1 = Math.max(x1, p[2]); y0 = Math.min(y0, p[yi]); y1 = Math.max(y1, p[yi]); }
  const pad = 20, sx = (w - 2 * pad) / ((x1 - x0) || 1), sy = (h - 2 * pad) / ((y1 - y0) || 1), s = Math.min(sx, sy);
  const ox = pad + ((w - 2 * pad) - (x1 - x0) * s) / 2, oy = pad + ((h - 2 * pad) - (y1 - y0) * s) / 2;
  S.proj = (p) => [ox + (p[2] - x0) * s, h - (oy + (p[yi] - y0) * s)];
  g.globalCompositeOperation = "lighter";
  for (const p of pts) { const [X, Y] = S.proj(p); g.fillStyle = typeColor(p[1]); g.globalAlpha = 0.7; g.fillRect(X - 1, Y - 1, 2.2, 2.2); }
  g.globalAlpha = 1; g.globalCompositeOperation = "source-over";
  g.fillStyle = "#8a97b6"; g.font = "12px sans-serif";
  g.fillText(`${fmt(pts.length)} 个对象 · x ∈ [${fmt(x0)}, ${fmt(x1)}]`, 12, h - 12);
}
$("#sky").onclick = (e) => {
  if (!S.proj) return;
  const r = e.target.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
  let best = null, bd = 64;
  for (const p of S.mapPts) { const [X, Y] = S.proj(p); const d = (X - mx) ** 2 + (Y - my) ** 2; if (d < bd) { bd = d; best = p; } }
  if (best) showObject(`${S.test}-${best[0]}`);
};
$("#frame").onchange = renderMap;
$("#proj").onchange = drawMap;

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
    $("#recon").innerHTML = `<h3>${r.ok ? '<span class="badge ok">全部一致</span>' : '<span class="badge bad">发现不一致</span>'} ${esc(r.test_id)}</h3>` +
      row("发生器已发送批次", fmt(b.sent)) + row("我们确认（2xx）的批次", fmt(b.acked_by_us)) + row("实际落盘批次", fmt(b.stored)) +
      row("已确认但未落盘（丢失）", fmt(b.acked_but_missing), b.acked_but_missing === 0) +
      row("落盘但从未发送（幻影）", fmt(b.stored_never_sent), b.stored_never_sent === 0) +
      row("落盘但确认未送达（可接受）", fmt(b.stored_without_ack)) +
      row("对象：发生器确认 / 库中行数", `${fmt(o.generator_acknowledged)} / ${fmt(o.rows_in_store)}`, o.rows_in_store === o.sum_of_stored_batches) +
      row("故障消息：发送 / 拒收 / 误收", `${fa.sent} / ${fa.rejected} / ${fa.acked}`, fa.acked === 0 && fa.stored === 0) +
      row("重复批次：发送 / 吸收", `${du.sent} / ${du.absorbed_by_store}`) +
      row("类型树修订：发生器确认 / 已存", `${t.generator_acknowledged} / ${t.stored}`, t.stored >= t.generator_acknowledged);
  } catch (err) { $("#recon").innerHTML = `<p class="badge bad">${esc(err.message)}</p>`; }
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
    else if (S.tab === "map") { if (force) await renderMap(); }
    else if (S.tab === "evolve") await renderEvolve();
    else if (S.tab === "trust") await renderTrust();
    $("#live").classList.remove("off");
  } catch (e) { console.warn(e); $("#live").classList.add("off"); }
  finally { busy = false; }
}
let tick = 0;
setInterval(() => { tick++; if (S.tab === "overview" || (S.tab === "trust" && tick % 3 === 0)) refresh(false); if (tick % 10 === 0) { loadTests(); S.tree = null; } }, 2000);
refresh(true);
