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

/* ========== 批量选择管理器 ========== */

/**
 * 通用多选管理器（工厂函数）
 * @param {Object} opts
 * @param {string} opts.checkboxSel  - 行 checkbox 的 CSS 选择器（如 '.row-check'）
 * @param {string} opts.headerCheckId - 表头全选 checkbox 的 id（可选）
 * @param {string} opts.btnId         - 批量删除按钮 id
 * @param {string} opts.countId       - 选中计数显示 id
 * @param {Function} opts.getLabel    - (checkboxEl) => 用于弹窗摘要的文本
 * @param {Function} [opts.onChange]  - 选中变化时的回调（可选）
 * @returns {{ getSelected: () => string[], clear: () => void, refresh: () => void }}
 */
function createBulkSelector(opts) {
  var header = opts.headerCheckId ? document.getElementById(opts.headerCheckId) : null;
  var btn = document.getElementById(opts.btnId);
  var countEl = document.getElementById(opts.countId);

  function _checkboxes() {
    return Array.from(document.querySelectorAll(opts.checkboxSel));
  }

  function _enabled(boxes) {
    return boxes.filter(function (cb) { return !cb.disabled; });
  }

  function update() {
    var boxes = _checkboxes();
    var enabled = _enabled(boxes);
    var checked = enabled.filter(function (cb) { return cb.checked; });
    // 更新计数
    if (countEl) countEl.textContent = checked.length > 0 ? "已选 " + checked.length + " 项" : "";
    // 更新按钮状态
    if (btn) {
      btn.disabled = checked.length === 0;
      if (checked.length > 0) { btn.classList.add("active"); } else { btn.classList.remove("active"); }
    }
    // 更新表头全选状态
    if (header) {
      header.checked = enabled.length > 0 && checked.length === enabled.length;
      header.indeterminate = checked.length > 0 && checked.length < enabled.length;
    }
    if (opts.onChange) opts.onChange(checked);
  }

  // 表头全选事件
  if (header) {
    header.addEventListener("change", function () {
      var enabled = _enabled(_checkboxes());
      enabled.forEach(function (cb) { cb.checked = header.checked; });
      update();
    });
  }

  // 行 checkbox 事件（事件委托：监听 tbody 或列表容器）
  // 需要调用方在渲染后确保 checkbox 有 onchange
  // 这里提供一个便捷的绑定方法
  function _bindCheckboxes() {
    _checkboxes().forEach(function (cb) {
      if (!cb._bulkBound) {
        cb.addEventListener("change", update);
        cb._bulkBound = true;
      }
    });
  }

  return {
    getSelected: function () {
      return _checkboxes().filter(function (cb) { return cb.checked && !cb.disabled; }).map(function (cb) { return cb.value; });
    },
    getLabels: function () {
      return _checkboxes().filter(function (cb) { return cb.checked && !cb.disabled; }).map(opts.getLabel);
    },
    clear: function () {
      _checkboxes().forEach(function (cb) { cb.checked = false; });
      if (header) header.checked = false;
      update();
    },
    refresh: function () {
      _bindCheckboxes();
      update();
    },
    update: update,
  };
}

/**
 * 打开批量删除确认弹窗
 * @param {string[]} labels   - 待删除项的摘要文本
 * @param {string} typeLabel  - 数据类型名（"批次"/"产品映射" 等）
 * @param {Function} onConfirm - async () => Promise<void>
 */
function openBulkDeleteModal(labels, typeLabel, onConfirm) {
  var mask = document.getElementById("bulk-delete-mask");
  if (!mask) {
    // 动态创建弹窗（仅创建一次）
    mask = document.createElement("div");
    mask.id = "bulk-delete-mask";
    mask.className = "modal-mask";
    mask.onclick = function (e) { if (e.target === mask) closeBulkDeleteModal(); };
    mask.innerHTML =
      '<div class="modal">' +
      '<h3 id="bulk-del-title"></h3>' +
      '<p style="font-size:13px;color:#4b5563;margin:0 0 6px">将删除以下记录（不可恢复）：</p>' +
      '<ul id="bulk-del-items" class="bulk-del-list"></ul>' +
      '<ul class="del-list">' +
      '<li><strong>将保留：</strong>审计记录、工厂会话、输出文件</li>' +
      '</ul>' +
      '<div class="modal-actions">' +
      '<button class="btn" onclick="closeBulkDeleteModal()">取消</button>' +
      '<button id="bulk-del-confirm-btn" class="btn btn-danger" onclick="_bulkDelConfirm()">确认删除</button>' +
      '</div>' +
      '</div>';
    document.body.appendChild(mask);
  }
  document.getElementById("bulk-del-title").textContent = "批量删除 " + labels.length + " 条" + typeLabel + "？";
  var maxShow = 10;
  var itemsHtml = labels.slice(0, maxShow).map(function (l) { return "<li>• " + esc(l) + "</li>"; }).join("");
  if (labels.length > maxShow) {
    itemsHtml += '<li class="bulk-del-more">…还有 ' + (labels.length - maxShow) + ' 条</li>';
  }
  document.getElementById("bulk-del-items").innerHTML = itemsHtml;
  var btn = document.getElementById("bulk-del-confirm-btn");
  btn.disabled = false;
  btn.textContent = "确认删除";
  // 存储回调
  window._bulkDelCallback = onConfirm;
  mask.classList.add("show");
}

function closeBulkDeleteModal() {
  var mask = document.getElementById("bulk-delete-mask");
  if (mask) mask.classList.remove("show");
  window._bulkDelCallback = null;
}

async function _bulkDelConfirm() {
  if (!window._bulkDelCallback) return;
  var btn = document.getElementById("bulk-del-confirm-btn");
  btn.disabled = true;
  btn.textContent = "删除中…";
  try {
    await window._bulkDelCallback();
    closeBulkDeleteModal();
  } catch (e) {
    toast("删除失败：" + e.message, 4000);
    btn.disabled = false;
    btn.textContent = "确认删除";
  }
}
