/* ============================================================
   雅玛多单证自动化 - 共享前端工具（ui.js）
   纯原生 JS，直接挂 window，无模块、无构建。
   提供：$ / esc / toast / api / renderTopbar / fmtTime
   ============================================================ */
"use strict";

/* ---------- DOM 快捷 ---------- */
const $ = (id) => document.getElementById(id);

/* ---------- HTML 转义（防 XSS） ---------- */
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));

/* ---------- Toast（底部居中淡入） ---------- */
let _toastTimer = null;
function toast(msg, ms = 2600) {
  let t = $("toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "toast";
    document.body.appendChild(t);
  }
  t.textContent = msg;
  // 强制重排以便连续触发时重新播放过渡
  t.classList.remove("show");
  void t.offsetWidth;
  t.classList.add("show");
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => t.classList.remove("show"), ms);
}

/* ---------- fetch 封装：JSON 请求/响应，统一错误 ---------- */
async function api(path, { method = "GET", body } = {}) {
  let r;
  try {
    r = await fetch(path, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new Error("网络错误：无法连接服务器，请检查后端服务是否在运行");
  }
  let data = null;
  const text = await r.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (e) {
      data = null;
    }
  }
  if (!r.ok) {
    let detail = data && data.detail;
    if (Array.isArray(detail)) {
      // FastAPI 422 校验错误格式：[{loc, msg, ...}]
      detail = detail
        .map((d) => ((d.loc || []).slice(-1)[0] || "") + ": " + (d.msg || ""))
        .join("；");
    }
    if (typeof detail !== "string" || !detail) {
      detail = "HTTP " + r.status;
    }
    const err = new Error(detail);
    err.status = r.status;
    throw err;
  }
  return data;
}

/* ---------- 顶栏渲染：插入到 body 开头 ---------- */
function renderTopbar(active) {
  const bar = document.createElement("div");
  bar.className = "topbar";
  const links = [
    { key: "dashboard", text: "工作台", href: "/" },
    { key: "chat", text: "Agent对话", href: "/chat" },
    { key: "mappings", text: "主数据维护", href: "/mappings" },
  ];
  const navHtml = links
    .map(
      (l) =>
        `<a href="${l.href}"${l.key === active ? ' class="active"' : ""}>${esc(l.text)}</a>`
    )
    .join("");
  bar.innerHTML =
    `<span class="title">雅玛多单证自动化</span>` +
    `<nav class="nav">${navHtml}</nav>` +
    `<span class="spacer"></span>` +
    `<span id="topbar-slot"></span>`;
  document.body.insertBefore(bar, document.body.firstChild);
  return bar;
}

/* ---------- 时间格式化：ISO 字符串或 epoch 秒 → "MM-DD HH:mm" ---------- */
function fmtTime(v) {
  if (v === null || v === undefined || v === "") return "-";
  let d;
  if (typeof v === "number") {
    // epoch 秒（>1e12 视为毫秒容错）
    d = new Date(v > 1e12 ? v : v * 1000);
  } else {
    const s = String(v);
    // SQLite "YYYY-MM-DD HH:MM:SS" 格式补 T，避免 Safari 解析失败
    d = new Date(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(s) ? s.replace(" ", "T") : s);
  }
  if (isNaN(d.getTime())) return "-";
  const pad = (n) => String(n).padStart(2, "0");
  return (
    pad(d.getMonth() + 1) +
    "-" +
    pad(d.getDate()) +
    " " +
    pad(d.getHours()) +
    ":" +
    pad(d.getMinutes())
  );
}
