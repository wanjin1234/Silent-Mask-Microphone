/* radarpi Web 上位机前端
 *
 * 数据来自 /api/stream 的 SSE：
 *   event: hello/status -> 状态对象
 *   event: frame        -> 一帧点云
 *
 * 渲染策略：SSE 回调只更新最新帧并置脏标记，真正的绘制放在
 * requestAnimationFrame 里按屏幕刷新率节流，避免高帧率时把树莓派的
 * 单核 CPU 打满。
 */

"use strict";

const GROUP_COLORS = ["#ffd23f", "#8a8a5c", "#37c8ff", "#2b6f8a", "#7cff6b", "#3f7a3a"];
const GROUP_SHORT = ["动高", "动低", "长微高", "长微低", "短微高", "短微低"];

const state = {
  frame: null,
  status: null,
  lastFrameAt: 0,
  filter: "all",
  paused: false,
  dirty: true,
  recording: false,
  // 默认视角：从左前方俯视，房间轮廓最清楚
  view: { yaw: -0.62, pitch: 0.5, dist: 7.2, zoom: 1.0 },
  drag: null,
  tableFollow: true,
  groups: [],
};

/* ------------------------------------------------------------------ 工具 */

const $ = (id) => document.getElementById(id);

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}

function setText(id, text) {
  const el = $(id);
  if (el && el.textContent !== text) el.textContent = text;
}

/* --------------------------------------------------------------- 画布基础 */

function setupCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(canvas.clientWidth, 10);
  const h = Math.max(canvas.clientHeight, 10);
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

function drawGrid(ctx, w, h, toPx, opts) {
  const { xRange, yRange, xLabel, yLabel } = opts;
  ctx.save();
  ctx.lineWidth = 1;
  ctx.strokeStyle = "#262c35";
  for (let v = Math.ceil(xRange[0]); v <= xRange[1]; v++) {
    const a = toPx(v, yRange[0]), b = toPx(v, yRange[1]);
    ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
  }
  for (let v = Math.ceil(yRange[0]); v <= yRange[1]; v++) {
    const a = toPx(xRange[0], v), b = toPx(xRange[1], v);
    ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
  }
  // 坐标轴
  ctx.strokeStyle = "#3b4450";
  const x0 = toPx(0, yRange[0]), x1 = toPx(0, yRange[1]);
  const y0 = toPx(xRange[0], 0), y1 = toPx(xRange[1], 0);
  ctx.beginPath(); ctx.moveTo(x0[0], x0[1]); ctx.lineTo(x1[0], x1[1]); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(y0[0], y0[1]); ctx.lineTo(y1[0], y1[1]); ctx.stroke();

  ctx.fillStyle = "#8b95a5";
  ctx.font = "11px sans-serif";
  ctx.textAlign = "left";
  for (let v = Math.ceil(xRange[0]); v <= xRange[1]; v++) {
    const p = toPx(v, yRange[0]);
    if (p[0] > 14 && p[0] < w - 30) ctx.fillText(String(v), p[0] - 4, h - 4);
  }
  // 轴名靠边对齐，避免超出画布
  ctx.textAlign = "right";
  ctx.fillText(xLabel, w - 5, h - 4);
  ctx.textAlign = "left";
  ctx.fillText(yLabel, 6, 13);
  ctx.restore();
}

/* ----------------------------------------------------------- 3D 投影视图 */

function project3d(x, y, z, w, h) {
  const v = state.view;
  const cy = Math.cos(v.yaw), sy = Math.sin(v.yaw);
  const cp = Math.cos(v.pitch), sp = Math.sin(v.pitch);
  // 绕 Z 轴偏航
  let px = x * cy - y * sy;
  let py = x * sy + y * cy;
  let pz = z - 1.0; // 视线中心略高于地面
  // 绕 X 轴俯仰
  const ry = py * cp + pz * sp;
  const rz = -py * sp + pz * cp;
  const depth = ry + v.dist;
  if (depth <= 0.25) return null;
  // 焦距按画布短边与长边综合考虑，宽画布时也能把房间填满
  const focal = Math.min(w, h * 1.35) * 1.05 * v.zoom;
  const s = focal / depth;
  return { x: w / 2 + px * s, y: h / 2 - rz * s, s, depth };
}

function render3d(frame) {
  const canvas = $("cv-3d");
  const { ctx, w, h } = setupCanvas(canvas);

  // 地面网格
  ctx.save();
  ctx.strokeStyle = "#252b34";
  for (let i = -4; i <= 4; i++) {
    let a = project3d(i, -1, 0, w, h), b = project3d(i, 6, 0, w, h);
    if (a && b) { ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke(); }
    a = project3d(-4, i, 0, w, h); b = project3d(4, i, 0, w, h);
    if (a && b) { ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke(); }
  }
  // 世界坐标轴
  const axes = [
    [[0, 0, 0], [1.2, 0, 0], "#e06c75", "X"],
    [[0, 0, 0], [0, 1.2, 0], "#98c379", "Y"],
    [[0, 0, 0], [0, 0, 1.2], "#61afef", "Z"],
  ];
  ctx.lineWidth = 1.6;
  for (const [p0, p1, color, label] of axes) {
    const a = project3d(p0[0], p0[1], p0[2], w, h), b = project3d(p1[0], p1[1], p1[2], w, h);
    if (!a || !b) continue;
    ctx.strokeStyle = color;
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    ctx.fillStyle = color;
    ctx.font = "12px sans-serif";
    ctx.fillText(label, b.x + 3, b.y - 3);
  }
  ctx.restore();

  // 雷达位置（原点，离地 1.7m 由高度参数决定，这里画在 0 便于判读）
  if (frame) drawPoints3d(ctx, frame, w, h);
}

function visiblePoints(frame) {
  if (!frame) return [];
  const f = state.filter;
  let pts = frame.points || [];
  if (f === "dynamic") pts = pts.filter((p) => p.g <= 1);
  else if (f === "micro") pts = pts.filter((p) => p.g >= 2);
  else if (f === "tracks") pts = [];
  return pts;
}

function drawPoints3d(ctx, frame, w, h) {
  const pts = visiblePoints(frame);
  // 按深度排序，远的先画，近的后画，形成遮挡感
  const projected = [];
  for (const p of pts) {
    const q = project3d(p.x, p.y, p.z, w, h);
    if (q) projected.push([q, p]);
  }
  projected.sort((a, b) => b[0].depth - a[0].depth);
  for (const [q, p] of projected) {
    const dist = Math.min(Math.max(q.depth, 0.5), 20);
    const base = Math.max(1.6, (q.s / 26));
    const alpha = p.g % 2 === 0 ? 0.95 : 0.45;
    ctx.globalAlpha = alpha * (1 - Math.min(dist / 40, 0.55));
    ctx.fillStyle = GROUP_COLORS[p.g] || "#fff";
    ctx.beginPath();
    ctx.arc(q.x, q.y, base, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.globalAlpha = 1;

  // 航迹
  for (const t of frame.tracks || []) {
    const q = project3d(t.x, t.y, t.z, w, h);
    if (!q) continue;
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 1.4;
    ctx.beginPath(); ctx.arc(q.x, q.y, 6, 0, Math.PI * 2); ctx.stroke();
    ctx.fillStyle = "#ffffff";
    ctx.font = "11px sans-serif";
    ctx.fillText("T" + t.id, q.x + 8, q.y + 4);
  }
}

/* --------------------------------------------------------- 正交投影视图 */

function renderOrtho(canvasId, frame, cfg) {
  const canvas = $(canvasId);
  const { ctx, w, h } = setupCanvas(canvas);
  const pad = 22;
  const xr = cfg.xRange, yr = cfg.yRange;
  const sx = (w - pad * 2) / (xr[1] - xr[0]);
  const sy = (h - pad * 2) / (yr[1] - yr[0]);
  const s = Math.min(sx, sy);
  const ox = pad + ((w - pad * 2) - (xr[1] - xr[0]) * s) / 2;
  const oy = pad + ((h - pad * 2) - (yr[1] - yr[0]) * s) / 2;
  const toPx = (a, b) => [ox + (a - xr[0]) * s, h - oy - (b - yr[0]) * s];

  ctx.fillStyle = "#171b21";
  ctx.fillRect(0, 0, w, h);
  drawGrid(ctx, w, h, toPx, { xRange: xr, yRange: yr, xLabel: cfg.xLabel, yLabel: cfg.yLabel });

  if (!frame) return;
  const pts = state.filter === "tracks" ? [] : visiblePoints(frame);
  for (const p of pts) {
    const [a, b] = cfg.map(p);
    const [px, py] = toPx(a, b);
    ctx.globalAlpha = p.g % 2 === 0 ? 0.95 : 0.5;
    ctx.fillStyle = GROUP_COLORS[p.g] || "#fff";
    ctx.beginPath();
    ctx.arc(px, py, p.g <= 1 ? 2.4 : 2.0, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.globalAlpha = 1;
  for (const t of frame.tracks || []) {
    const [a, b] = cfg.map(t);
    const [px, py] = toPx(a, b);
    ctx.strokeStyle = "#fff";
    ctx.lineWidth = 1.4;
    ctx.beginPath(); ctx.arc(px, py, 5, 0, Math.PI * 2); ctx.stroke();
  }
}

/* ---------------------------------------------------------------- 面板 */

function updateStats() {
  const st = state.status;
  const fr = state.frame;
  if (st && st.source) {
    const src = st.source;
    const connected = !!src.connected;
    const dot = $("link-dot");
    dot.className = "dot " + (connected ? (staleFrame() ? "warn" : "ok") : "");
    setText("link-text", connected ? "已连接" : "未连接");
    const dev = src.device || "-";
    setText("device-text", (src.kind === "serial" ? dev : src.kind) + (src.baudrate ? " @" + src.baudrate : ""));
    setText("st-clients", String(st.web ? st.web.clients : "-"));
    setText("st-rate", (src.bytes_per_sec ? (src.bytes_per_sec / 1024).toFixed(1) + " KB/s" : "-"));
    setText("st-fps", (st.web ? st.web.frame_fps.toFixed(1) : "-") + " fps");
    setText("sb-source", "数据源：" + (src.kind || "-") + " " + (dev || ""));
    const pm = src.parse || {};
    setText("sb-parse", `解析：帧 ${pm.frames || 0} · 重同步 ${pm.resyncs || 0} · 丢弃 ${pm.dropped_bytes || 0} B`);
    const warn = $("parse-warn");
    if ((pm.resyncs || 0) > 20 || (pm.incomplete_frames || 0) > 0) {
      warn.classList.remove("hidden");
      warn.textContent = `链路告警：重同步 ${pm.resyncs} 次、残帧 ${pm.incomplete_frames || 0} 帧。` +
        `波特率或串口小板可能不匹配（需支持 3000000 的 FT232/CH343/CH344）。`;
    } else {
      warn.classList.add("hidden");
    }
    if (st.record && st.record.recording) {
      state.recording = true;
      setText("record-text", "→ " + (st.record.path || "").split(/[\\/]/).pop());
      $("btn-record").textContent = "停止录制";
      $("btn-record").classList.add("active");
    } else {
      state.recording = false;
      $("btn-record").textContent = "开始录制";
      $("btn-record").classList.remove("active");
      if (st.record && st.record.frames) setText("record-text", `已完成 ${st.record.frames} 帧`);
    }
  }

  if (fr) {
    const hdr = fr.header;
    setText("st-frame", "#" + hdr.frame_id);
    setText("st-points", String((fr.points || []).length) + (fr.truncated ? "（已抽样）" : ""));
    setText("st-period", (hdr.frame_period || 0) + " ms");
    setText("st-interval", (hdr.frame_interval || 0) + " ms");
    setText("st-bb", (hdr.bb_time || 0) + " ms");
    setText("st-postbb", (hdr.post_bb_time || 0) + " ms");
    setText("st-transfer", (hdr.transfer_time || 0) + " ms");
    setText("sb-last", "最近一帧：" + fmtTime(fr.ts) + "（#" + hdr.frame_id + "，本地 " +
      (Date.now() / 1000 - fr.ts).toFixed(1) + " s 前）");
  }
  setText("clock", new Date().toLocaleTimeString("zh-CN", { hour12: false }));
}

function updateGroupCounts() {
  const tbody = $("group-counts");
  if (!tbody) return;
  const counts = state.frame ? state.frame.header.counts : {};
  const groups = state.groups.length
    ? state.groups
    : GROUP_SHORT.map((s, i) => ({ index: i, name: s, short: s, color: GROUP_COLORS[i], key: "g" + i }));
  let html = "";
  for (const g of groups) {
    const n = counts[g.key] !== undefined ? counts[g.key] : 0;
    html += `<tr><td><span class="swatch" style="background:${g.color}"></span></td>` +
      `<td>${g.name}</td><td>${n}</td></tr>`;
  }
  html += `<tr><td><span class="swatch" style="background:#ffffff"></span></td><td>航迹</td>` +
    `<td>${counts.tracks || 0}</td></tr>`;
  tbody.innerHTML = html;
}

function updateTable() {
  if (!state.tableFollow || state.paused) return;
  const tbody = $("point-body");
  const fr = state.frame;
  if (!fr || !fr.points) { tbody.innerHTML = ""; return; }
  const maxRows = 300;
  const rows = fr.points.slice(0, maxRows);
  const parts = [];
  for (let i = 0; i < rows.length; i++) {
    const p = rows[i];
    const c = GROUP_COLORS[p.g] || "#fff";
    parts.push(
      `<tr><td style="color:${c}">${i + 1}</td><td>${p.x.toFixed(3)}</td><td>${p.y.toFixed(3)}</td>` +
      `<td>${p.z.toFixed(3)}</td><td>${p.v.toFixed(2)}</td><td>${p.snr}</td>` +
      `<td style="color:${c}">${GROUP_SHORT[p.g] || p.g}</td></tr>`
    );
  }
  tbody.innerHTML = parts.join("");
  setText("table-note", `共 ${fr.header.counts.points} 点，显示前 ${rows.length} 个` +
    (fr.truncated ? "（推送已抽样，完整数据见录制文件）" : ""));
}

function staleFrame() {
  return state.lastFrameAt > 0 && (Date.now() / 1000 - state.lastFrameAt) > 2.5;
}

/* ---------------------------------------------------------------- 控制 */

async function postJson(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  try { return await resp.json(); } catch (e) { return { ok: false, error: "响应解析失败" }; }
}

function toast(msg, isError) {
  const el = $("sb-msg");
  el.textContent = msg || "";
  el.style.color = isError ? "var(--err)" : "var(--fg-dim)";
  if (msg) setTimeout(() => { if (el.textContent === msg) el.textContent = ""; }, 5000);
}

function bindControls() {
  $("btn-start").onclick = async () => {
    const r = await postJson("/api/command", { cmd: "scan start" });
    toast(r.ok ? "已发送 scan start" : "发送失败：" + r.error, !r.ok);
  };
  $("btn-stop").onclick = async () => {
    const r = await postJson("/api/command", { cmd: "scan stop" });
    toast(r.ok ? "已发送 scan stop" : "发送失败：" + r.error, !r.ok);
  };
  $("btn-clear").onclick = () => { state.frame = null; state.dirty = true; };
  $("btn-pause").onclick = (e) => {
    state.paused = !state.paused;
    e.target.textContent = state.paused ? "继续" : "暂停";
    e.target.classList.toggle("active", state.paused);
  };
  $("view-select").onchange = (e) => { state.filter = e.target.value; state.dirty = true; };
  $("tbl-follow").onchange = (e) => { state.tableFollow = e.target.checked; };
  $("btn-record").onclick = async () => {
    const action = state.recording ? "stop" : "start";
    const r = await postJson("/api/record", { action });
    toast(r.ok ? (action === "start" ? "开始录制 → " + r.path : "录制已停止") : "操作失败：" + r.error, !r.ok);
    refreshStatus();
  };
  $("btn-config").onclick = () => {
    const d = $("drawer");
    d.classList.toggle("hidden");
    if (!d.classList.contains("hidden")) loadConfig();
  };
  $("drawer-close").onclick = () => $("drawer").classList.add("hidden");
  $("raw-send").onclick = async () => {
    const text = $("raw-cmd").value.trim();
    if (!text) return;
    const r = await postJson("/api/command", { cmd: text });
    toast(r.ok ? "已发送：" + r.cmd : "发送失败：" + r.error, !r.ok);
  };

  // 3D 视图交互
  const canvas = $("cv-3d");
  canvas.addEventListener("mousedown", (e) => {
    state.drag = { x: e.clientX, y: e.clientY, yaw: state.view.yaw, pitch: state.view.pitch };
  });
  window.addEventListener("mousemove", (e) => {
    if (!state.drag) return;
    const dx = e.clientX - state.drag.x, dy = e.clientY - state.drag.y;
    state.view.yaw = state.drag.yaw + dx * 0.008;
    state.view.pitch = Math.max(-0.4, Math.min(1.4, state.drag.pitch + dy * 0.006));
    state.dirty = true;
  });
  window.addEventListener("mouseup", () => { state.drag = null; });
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    state.view.zoom = Math.max(0.3, Math.min(4.0, state.view.zoom * (e.deltaY > 0 ? 0.92 : 1.08)));
    state.dirty = true;
  }, { passive: false });

  window.addEventListener("resize", () => { state.dirty = true; });
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") $("drawer").classList.add("hidden");
  });
}

/* ------------------------------------------------------------ 设置抽屉 */

async function loadConfig() {
  const resp = await fetch("/api/config");
  const data = await resp.json();
  setText("conf-path", data.path || "");
  const list = $("conf-list");
  const itemByKey = {};
  for (const it of data.items || []) itemByKey[it.section + "." + it.key] = it;

  let html = "";
  let section = null;
  for (const it of data.items || []) {
    if (it.section !== section) {
      section = it.section;
      html += `<div class="conf-title">[${section}] ${it.section_title}</div>`;
    }
    html += `<div class="conf-row">
        <label>${it.key}</label>
        <input type="text" data-section="${it.section}" data-key="${it.key}" value="${escapeAttr(it.value)}">
      </div>
      <div class="conf-row"><span class="conf-hint">${it.hint || ""}</span></div>`;
  }
  list.innerHTML = html;
  $("conf-save").onclick = async () => {
    const items = [];
    for (const input of list.querySelectorAll("input[data-key]")) {
      items.push({ section: input.dataset.section, key: input.dataset.key, value: input.value });
    }
    const r = await postJson("/api/config", { items, save: true });
    toast(r.ok ? "配置已保存到 " + r.saved : "保存失败：" + r.error, !r.ok);
  };

  // 雷达指令行
  const st = await (await fetch("/api/status")).json();
  const cmdList = $("cmd-list");
  let ch = "";
  for (const c of st.commands || []) {
    ch += `<div class="cmd-row">
        <label>${c.key}</label>
        <input type="text" data-cmd="${c.key}" value="${escapeAttr(c.args ? c.default : "")}"
               placeholder="${escapeAttr(c.args ? c.default : "无需参数")}" ${c.args ? "" : "disabled"}>
        <button data-cmd-send="${c.key}">下发</button>
      </div>
      <div class="cmd-row"><span class="cmd-desc">${c.title}：${c.desc} 指令：<code>${c.template}</code></span></div>`;
  }
  cmdList.innerHTML = ch;
  for (const btn of cmdList.querySelectorAll("button[data-cmd-send]")) {
    btn.onclick = async () => {
      const key = btn.dataset.cmdSend;
      const input = cmdList.querySelector(`input[data-cmd="${key}"]`);
      const values = input && input.value.trim() ? input.value.trim().split(/\s+/) : [];
      const r = await postJson("/api/command", { key, values });
      toast(r.ok ? "已下发：" + r.cmd : "下发失败：" + r.error, !r.ok);
    };
  }
}

function escapeAttr(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

async function refreshStatus() {
  try {
    const st = await (await fetch("/api/status")).json();
    applyStatus(st);
  } catch (e) { /* 忽略瞬时失败 */ }
}

function applyStatus(st) {
  state.status = st;
  if (st.groups) state.groups = st.groups;
  state.dirty = true;
  updateStats();
  updateGroupCounts();
}

/* ------------------------------------------------------------ SSE 连接 */

function connect() {
  const src = new EventSource("/api/stream");
  src.addEventListener("hello", (e) => applyStatus(JSON.parse(e.data)));
  src.addEventListener("status", (e) => applyStatus(JSON.parse(e.data)));
  src.addEventListener("frame", (e) => {
    const data = JSON.parse(e.data);
    state.frame = data;
    state.lastFrameAt = data.ts || Date.now() / 1000;
    state.dirty = true;
  });
  src.onerror = () => {
    setText("link-text", "连接中断，重试中…");
    $("link-dot").className = "dot warn";
  };
  src.onopen = () => refreshStatus();
  return src;
}

/* --------------------------------------------------------------- 主循环 */

const ORTHO_VIEWS = {
  "cv-xy": { xRange: [-4, 4], yRange: [0, 6], xLabel: "X(m)", yLabel: "Y(m)", map: (p) => [p.x, p.y] },
  "cv-xz": { xRange: [-4, 4], yRange: [0, 3.2], xLabel: "X(m)", yLabel: "Z(m)", map: (p) => [p.x, p.z] },
  "cv-yz": { xRange: [0, 6], yRange: [0, 3.2], xLabel: "Y(m)", yLabel: "Z(m)", map: (p) => [p.y, p.z] },
};

function tick() {
  if (state.dirty && !state.paused) {
    state.dirty = false;
    render3d(state.frame);
    for (const [id, cfg] of Object.entries(ORTHO_VIEWS)) renderOrtho(id, state.frame, cfg);
    updateStats();
    updateTable();
    updateGroupCounts();
  }
  requestAnimationFrame(tick);
}

async function init() {
  bindControls();
  await refreshStatus();
  connect();
  requestAnimationFrame(tick);
  // 网页推送可能被限制帧率，状态另用 2 秒一次的轮询兜底
  setInterval(() => { if (!state.paused) refreshStatus(); }, 2000);
  setInterval(() => { if (staleFrame() && !state.paused) { state.dirty = true; } }, 1000);
}

init();
