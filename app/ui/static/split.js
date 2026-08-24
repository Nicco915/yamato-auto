"use strict";

/* ============================================================
   分票审核页 — 前端交互逻辑
   依赖 ui.js 的 $ / esc / toast / api / renderTopbar / fmtTime
   ============================================================ */

/* ---------- 商检工厂判定 ---------- */
var SJ_RED = ['貝来', '贝来'];
var SJ_ORANGE = ['正達', '正达'];

function isSJRed(name) { return SJ_RED.some(function(p){ return (name||'').indexOf(p) !== -1; }); }
function isSJOrange(name) { return SJ_ORANGE.some(function(p){ return (name||'').indexOf(p) !== -1; }); }
function isSJFactory(name) { return isSJRed(name) || isSJOrange(name); }
function sjBadgeClass(name) {
  if (isSJRed(name)) return 'sj-badge-red';
  if (isSJOrange(name)) return 'sj-badge-orange';
  return '';
}

/* ---------- 全局状态 ---------- */
var proposal = null;          // SplitProposal dict
var splitThreadId = null;     // /split/{split_thread_id}
var batchThreadId = null;     // batch thread_id（从 split_thread_id 推导）
var sourceFile = "";          // 源文件名
var status = "pending_review";
var originalProposal = null;  // 深拷贝初始方案，备用
var activePort = "";          // 当前选中港口
var dragSrc = null;           // {ticketIdx: number, itemIdx: number} 拖拽源
var declFiles = [];           // 已生成的报关单文件列表（/files）

/* ---------- 初始化 ---------- */
async function init() {
    try {
        var m = location.pathname.match(/\/split\/(.+)$/);
        splitThreadId = m ? decodeURIComponent(m[1].replace(/\/+$/, "")) : "";
    } catch(e) { splitThreadId = ""; }

    if (!splitThreadId) {
        showError('URL 中未包含 split_thread_id（应为 /split/{split_thread_id}）');
        return;
    }

    // 从 split_thread_id 推导 batch thread_id（惯例：split-{batch}）
    batchThreadId = splitThreadId.replace(/^split-/, '');

    renderTopbar('分票审核');

    try {
        await loadProposal();
    } catch(e) {
        // loadProposal 已调 showError
        return;
    }
    render();
}

/* ---------- API ---------- */
async function loadProposal() {
    var data = await api('/api/v1/split/' + encodeURIComponent(splitThreadId) + '/proposal');
    splitThreadId = data.split_thread_id || splitThreadId;
    sourceFile = data.source_file || '';
    status = data.status || 'pending_review';
    proposal = data.proposal || { ports: [] };
    originalProposal = JSON.parse(JSON.stringify(proposal));

    var ports = proposal.ports || [];
    if (ports.length > 0 && !activePort) activePort = ports[0].port;

    // 顺带查已生成的报关单文件（决定生成区/文件列表的显隐）
    try {
        var f = await api('/api/v1/split/' + encodeURIComponent(splitThreadId) + '/files');
        declFiles = (f && f.files) || [];
    } catch(e) {
        declFiles = [];
    }
}

async function confirmSplitAction(force) {
    var btn = document.getElementById('btn-confirm');
    btn.disabled = true;
    btn.textContent = '确认中…';
    try {
        // 确认时填了发票号码段 → 后端落库后直接生成报关单；留空则稍后补生成
        var inv = (document.getElementById('invoice-input').value || '').trim();
        if (inv) proposal.invoice_number = inv;
        else delete proposal.invoice_number;
        var body = { proposal: proposal, force: !!force };
        var data = await api('/api/v1/split/' + encodeURIComponent(splitThreadId) + '/confirm', {
            method: 'POST', body: body
        });
        status = data.status || 'confirmed';
        toast(inv ? '分票方案已确认，报关单生成中/已生成' : '分票方案已确认（未填号码段，可稍后生成报关单）');
        // 确认可能已触发生成：刷新方案与文件列表
        await loadProposal();
        render();
        return;
    } catch(e) {
        toast('确认失败：' + e.message, 4000);
        btn.disabled = false;
        btn.textContent = '确认分票';
    }
}

/* 确认后补生成：填发票号码段 → POST /generate */
async function generateDeclarations() {
    var btn = document.getElementById('btn-generate');
    var inv = (document.getElementById('invoice-input').value || '').trim();
    if (!inv) {
        toast('请先填写发票号码段（如 656）');
        return;
    }
    btn.disabled = true;
    btn.textContent = '生成中…';
    try {
        var data = await api('/api/v1/split/' + encodeURIComponent(splitThreadId) + '/generate', {
            method: 'POST', body: { invoice_number: inv }
        });
        toast('已生成 ' + (data.count != null ? data.count : '?') + ' 票报关单');
        await loadProposal();   // 刷新文件列表与状态
        render();
        return;
    } catch(e) {
        toast('生成失败：' + e.message, 4000);
        btn.disabled = false;
        btn.textContent = '生成报关单';
    }
}

async function resetSplitAction() {
    var btn = document.getElementById('btn-reset');
    btn.disabled = true;
    btn.textContent = '重置中…';
    try {
        await api('/api/v1/split/' + encodeURIComponent(splitThreadId) + '/reset', { method: 'POST' });
        status = 'reset';
        toast('方案已重置');
        // 重置后刷新方案数据
        await loadProposal();
        render();
    } catch(e) {
        toast('重置失败：' + e.message, 4000);
        btn.disabled = false;
        btn.textContent = '重置分票';
    }
}

/* ---------- 渲染入口 ---------- */
function render() {
    renderHeader();
    renderTabs();
    renderSplitView();
    renderFiles();
    renderActions();
}

/* ---------- 头部 ---------- */
function renderHeader() {
    var sourceName = sourceFile ? (sourceFile.split(/[\\\/]/).pop() || sourceFile) : '-';
    var statusClassMap = {
        'pending_review': 'badge-warn',
        'confirmed': 'badge-ok',
        'reset': 'badge-muted',
        'completed': 'badge-ok',
        'declare_failed': 'badge-warn'
    };
    var statusTextMap = {
        'pending_review': '待审核',
        'confirmed': '已确认',
        'reset': '已重置',
        'completed': '已完成',
        'declare_failed': '生成失败'
    };
    var sc = statusClassMap[status] || 'badge-muted';
    var st = statusTextMap[status] || status;

    document.getElementById('title-source').textContent = sourceName;
    var badge = document.getElementById('status-badge');
    badge.textContent = st;
    badge.className = 'badge ' + sc;

    document.getElementById('batch-link').href = '/batch/' + encodeURIComponent(batchThreadId);
    document.getElementById('btn-back').href = '/batch/' + encodeURIComponent(batchThreadId);
}

/* ---------- 港口 Tab ---------- */
function renderTabs() {
    var ports = proposal.ports || [];
    var tabsEl = document.getElementById('port-tabs');
    if (ports.length === 0) {
        tabsEl.innerHTML = '<span class="muted" style="padding:8px 0">无港口数据</span>';
        return;
    }

    tabsEl.innerHTML = ports.map(function(p, i) {
        var active = p.port === activePort ? ' active' : '';
        return '<button class="port-tab' + active + '" onclick="switchPort(' + esc(JSON.stringify(p.port)) + ')">' + esc(p.port) + '</button>';
    }).join('');
}

function switchPort(port) {
    activePort = port;
    renderTabs();
    renderSplitView();
}

/* ---------- 工具：取当前港口数据 ---------- */
function getActivePortData() {
    var ports = proposal.ports || [];
    for (var i = 0; i < ports.length; i++) {
        if (ports[i].port === activePort) return ports[i];
    }
    return { port: activePort, groups: [] };
}

/* 确保当前港口分组存在于 proposal.ports（新建票时可能尚无该港口分组） */
function ensureActivePortData() {
    if (!proposal.ports) proposal.ports = [];
    var portData = getActivePortData();
    var found = false;
    for (var i = 0; i < proposal.ports.length; i++) {
        if (proposal.ports[i].port === activePort) { found = true; break; }
    }
    if (!found) {
        proposal.ports.push(portData);
    }
    if (!portData.groups) portData.groups = [];
    return portData;
}

/* ---------- 可编辑守卫：仅待审核状态允许增/删/拖 ---------- */
function isEditable() {
    return status === 'pending_review';
}

/* ---------- 票号重编：按港口内数组顺序（与引擎规则 7 一致） ---------- */
function renumberTickets() {
    var ports = (proposal && proposal.ports) || [];
    for (var p = 0; p < ports.length; p++) {
        var groups = ports[p].groups || [];
        for (var i = 0; i < groups.length; i++) {
            groups[i].ticket_no = ports[p].port + '-' + ('0' + (i + 1)).slice(-2);
        }
    }
}

/* ---------- 新建票 ---------- */
function createTicket() {
    if (!isEditable()) { toast('当前状态不可编辑分票方案', 2500); return; }
    var portData = ensureActivePortData();
    // 空票暂无箱型：拖入第一个柜时采用该柜箱型，之后一票一箱型
    portData.groups.push({
        ticket_no: '',
        port: activePort,
        container_type: '',
        items: [],
        sj_factories: [],
        full_containers: 0,
        warnings: []
    });
    renumberTickets();
    render();
}

/* ---------- 删除票（仅空票可删，防误操作带 confirm） ---------- */
function deleteTicket(ti) {
    if (!isEditable()) { toast('当前状态不可编辑分票方案', 2500); return; }
    var tickets = getActivePortData().groups || [];
    var t = tickets[ti];
    if (!t) return;
    if ((t.items || []).length > 0) {
        toast('请先移出票内所有柜', 3000);
        return;
    }
    if (!confirm('确定删除票 ' + (t.ticket_no || '') + '？')) return;
    tickets.splice(ti, 1);
    renumberTickets();
    render();
}

/* ---------- 左栏：柜列表 ---------- */
function buildContainerMap(portData) {
    var map = {};
    var tickets = portData.groups || [];

    for (var ti = 0; ti < tickets.length; ti++) {
        var ticket = tickets[ti];
        for (var ii = 0; ii < (ticket.items || []).length; ii++) {
            var item = ticket.items[ii];
            var k = item.kanri_no;
            if (!map[k]) {
                map[k] = {
                    kanri_no: k,
                    container_type: ticket.container_type || '',
                    factories: {},
                    sj_factories: {},
                    full_assigned: false,
                    partial_count: 0,
                    ticket_refs: []
                };
            }
            map[k].ticket_refs.push(ti);

            var hasExclude = !!(item.factory_exclude && item.factory_exclude.length);
            if (item.factory_filter === null || item.factory_filter === undefined) {
                if (hasExclude) {
                    // 非商检剩余票：柜内商检工厂之外的行单独成票
                    map[k].partial_count++;
                } else {
                    map[k].full_assigned = true;
                    // 全柜：工厂信息来自 ticket.sj_factories（间接推断）
                    var sjs = ticket.sj_factories || [];
                    for (var s = 0; s < sjs.length; s++) {
                        map[k].factories[sjs[s]] = true;
                        if (isSJFactory(sjs[s])) map[k].sj_factories[sjs[s]] = true;
                    }
                }
            } else {
                map[k].factories[item.factory_filter] = true;
                if (isSJFactory(item.factory_filter)) {
                    map[k].sj_factories[item.factory_filter] = true;
                }
                if (item.is_partial) {
                    map[k].partial_count++;
                }
            }
        }
    }
    return map;
}

function renderContainers() {
    var portData = getActivePortData();
    var containerMap = buildContainerMap(portData);
    var entries = [];
    for (var k in containerMap) {
        if (Object.prototype.hasOwnProperty.call(containerMap, k)) {
            entries.push(containerMap[k]);
        }
    }
    // 按箱型→柜号排序
    entries.sort(function(a, b) {
        var ct = a.container_type.localeCompare(b.container_type);
        return ct !== 0 ? ct : a.kanri_no.localeCompare(b.kanri_no);
    });

    var el = document.getElementById('container-list');
    document.getElementById('container-count').textContent = entries.length ? '(' + entries.length + ' 柜)' : '';

    if (entries.length === 0) {
        el.innerHTML = '<p class="empty-hint">该港口无柜号数据</p>';
        return;
    }

    // 按箱型分组
    var grouped = {};
    for (var i = 0; i < entries.length; i++) {
        var ct = entries[i].container_type || '未知箱型';
        if (!grouped[ct]) grouped[ct] = [];
        grouped[ct].push(entries[i]);
    }

    var html = '';
    var ctKeys = Object.keys(grouped).sort();
    for (var g = 0; g < ctKeys.length; g++) {
        var ct = ctKeys[g];
        var containers = grouped[ct];
        html += '<div class="container-group"><div class="container-group-header">' + esc(ct) + '（' + containers.length + ' 柜）</div>';
        for (var j = 0; j < containers.length; j++) {
            var c = containers[j];
            var assigned = c.full_assigned || c.partial_count > 0;
            var cls = 'container-item' + (assigned ? ' assigned' : '');

            // 商检标签
            var sjHtml = '';
            var sjNames = Object.keys(c.sj_factories);
            for (var s = 0; s < sjNames.length; s++) {
                var bc = sjBadgeClass(sjNames[s]);
                if (bc) sjHtml += '<span class="sj-badge ' + bc + '" title="商检: ' + esc(sjNames[s]) + '">检</span>';
            }

            // 工厂摘要
            var factNames = Object.keys(c.factories);
            var factSummary = factNames.length > 0
                ? factNames.slice(0, 3).map(function(n){return esc(n);}).join('、') + (factNames.length > 3 ? '…' : '')
                : '—';

            // 半票标记
            var partialNote = '';
            if (c.partial_count > 0) {
                // 列出半票涉及的工厂
                var pf = [];
                for (var key in c.factories) {
                    if (c.sj_factories[key]) pf.push(key);
                }
                partialNote = ' <span class="warning-text">（双商检柜，已拆半票）</span>';
            }

            var assignMark = assigned ? '<span class="assigned-mark">已分配</span>' : '';

            // 柜级数值（M3 / 箱数），来自 proposal.container_stats
            var stats = (proposal.container_stats || {})[c.kanri_no] || {};
            var statParts = [];
            if (stats.m3 !== null && stats.m3 !== undefined) statParts.push(esc(String(stats.m3)) + ' m³');
            if (stats.pcs !== null && stats.pcs !== undefined) statParts.push(esc(String(stats.pcs)) + ' 箱');
            var statsHtml = statParts.length
                ? '<span class="container-stats">' + statParts.join(' / ') + '</span>'
                : '';

            html += '<div class="' + cls + '" data-kanri="' + esc(c.kanri_no) + '">'
                + '<span class="mono">' + esc(c.kanri_no) + '</span>'
                + sjHtml
                + statsHtml
                + '<span class="factory-summary">' + factSummary + partialNote + '</span>'
                + assignMark
                + '</div>';
        }
        html += '</div>';
    }
    el.innerHTML = html;
}

/* ---------- 右栏：票卡片 ---------- */
function renderTickets() {
    var portData = getActivePortData();
    var tickets = portData.groups || [];
    var el = document.getElementById('ticket-cards');
    var editable = isEditable();

    document.getElementById('ticket-count').textContent = tickets.length ? '(' + tickets.length + ' 票)' : '';

    if (tickets.length === 0) {
        el.innerHTML = '<p class="empty-hint">该港口无票方案</p>' + createBtnHtml(editable);
        return;
    }

    // 按箱型分组展示（保留原数组索引，供拖拽/删除定位；
    // 新建的空票 container_type 为空，归入「新票（未指定箱型）」组排在最后）
    var NEW_TICKET_CT = '新票（未指定箱型）';
    var grouped = {};
    for (var i = 0; i < tickets.length; i++) {
        var ct = tickets[i].container_type || NEW_TICKET_CT;
        if (!grouped[ct]) grouped[ct] = [];
        grouped[ct].push({ ticket: tickets[i], idx: i });
    }

    var html = '';
    var ctKeys = Object.keys(grouped).sort(function(a, b) {
        // 「新票」组固定排最后，避免显示顺序与原数组顺序错位
        if (a === NEW_TICKET_CT) return 1;
        if (b === NEW_TICKET_CT) return -1;
        return a < b ? -1 : (a > b ? 1 : 0);
    });
    for (var g = 0; g < ctKeys.length; g++) {
        var ct = ctKeys[g];
        var ctTickets = grouped[ct];
        html += '<div class="ticket-type-group"><div class="container-group-header">' + esc(ct) + '（' + ctTickets.length + ' 票）</div>';

        for (var t = 0; t < ctTickets.length; t++) {
            var ticket = ctTickets[t].ticket;
            var ti = ctTickets[t].idx;  // 原数组索引
            var warnings = ticket.warnings || [];
            var hasWarn = warnings.length > 0;

            // 商检标签
            var sjHtml = '';
            var sjs = ticket.sj_factories || [];
            for (var s = 0; s < sjs.length; s++) {
                var bc = sjBadgeClass(sjs[s]);
                if (bc) sjHtml += '<span class="sj-badge ' + bc + '" title="商检: ' + esc(sjs[s]) + '">检</span>';
            }

            // 警告图标
            var warnIcon = hasWarn ? '<span class="warning-icon" title="有 ' + warnings.length + ' 条警告">⚠</span>' : '';

            // 柜条目列表（仅待审核状态可拖拽）
            var itemsHtml = '';
            var items = ticket.items || [];
            for (var ii = 0; ii < items.length; ii++) {
                var item = items[ii];
                var partialNote = '';
                if (item.is_partial) {
                    if (item.factory_filter) {
                        partialNote = ' <span class="item-partial-note">（' + esc(item.factory_filter) + '部分）</span>';
                    } else if (item.factory_exclude && item.factory_exclude.length) {
                        // 非商检剩余票：factory_filter 为空、factory_exclude 非空
                        partialNote = ' <span class="item-partial-note">（非商检部分）</span>';
                    }
                }
                var dragAttrs = editable
                    ? ' draggable="true" ondragstart="handleDragStart(event)" ondragend="handleDragEnd(event)"'
                    : '';
                itemsHtml += '<div class="container-item-in-ticket"' + dragAttrs
                    + ' data-ticket-idx="' + ti + '" data-item-idx="' + ii + '">'
                    + '<span class="mono">' + esc(item.kanri_no) + '</span>'
                    + partialNote
                    + '</div>';
            }
            if (!itemsHtml) {
                // 空票占位：提示拖入柜（只读状态下不出现空票，兜底文案）
                itemsHtml = editable
                    ? '<div class="empty-ticket-hint">空票——拖入柜</div>'
                    : '<span class="muted" style="font-size:12px">无柜条目</span>';
            }

            // 警告脚注
            var warnHtml = '';
            if (hasWarn) {
                warnHtml = '<div class="ticket-card-footer">'
                    + warnings.map(function(w){ return '<div class="warning-text">⚠ ' + esc(w.message || '') + '</div>'; }).join('')
                    + '</div>';
            }

            // 删除按钮（仅待审核状态；非空票点击时 toast 拦截）
            var delBtn = editable
                ? '<button class="ticket-del-btn" title="删除该票（仅空票可删）" onclick="deleteTicket(' + ti + ')">×</button>'
                : '';

            html += '<div class="ticket-card" id="ticket-card-' + ti + '"'
                + ' ondragover="handleDragOver(event)" ondrop="handleDrop(event)" ondragleave="handleDragLeave(event)"'
                + ' data-ticket-idx="' + ti + '">'
                + '<div class="ticket-card-header">'
                + '<span class="ticket-ticket-no">' + esc(ticket.ticket_no) + '</span>'
                + sjHtml + warnIcon
                + '<span class="ticket-meta">整柜 ' + (ticket.full_containers != null ? ticket.full_containers : 0) + '</span>'
                + delBtn
                + '</div>'
                + '<div class="ticket-card-body" id="ticket-body-' + ti + '">' + itemsHtml + '</div>'
                + warnHtml
                + '</div>';
        }
        html += '</div>';
    }

    html += createBtnHtml(editable);
    el.innerHTML = html;
}

/* 「+ 新建票」按钮（当前港口票列表末尾，仅待审核状态） */
function createBtnHtml(editable) {
    if (!editable) return '';
    return '<div class="ticket-create-row">'
        + '<button class="btn btn-ghost" onclick="createTicket()">+ 新建票</button>'
        + '</div>';
}

function renderSplitView() {
    renderContainers();
    renderTickets();
}

/* ---------- 操作栏 ---------- */
function renderActions() {
    var totalWarnings = countTotalWarnings();
    updateWarningSummary(totalWarnings);

    var invoiceInput = document.getElementById('invoice-input');
    var btnConfirm = document.getElementById('btn-confirm');
    var btnGenerate = document.getElementById('btn-generate');
    var btnReset = document.getElementById('btn-reset');

    if (status === 'pending_review') {
        // 待审核：可填号码段（留空=确认后补生成）+ 确认；也可重置推翻推荐
        invoiceInput.style.display = '';
        btnConfirm.style.display = '';
        btnConfirm.disabled = false;
        btnConfirm.textContent = '确认分票';
        btnGenerate.style.display = 'none';
        btnReset.style.display = '';
        btnReset.disabled = false;
    } else if (status === 'confirmed' || status === 'completed' || status === 'declare_failed') {
        // 已确认：可重置；未生成文件时显示号码段 + 生成入口
        btnConfirm.style.display = 'none';
        btnReset.style.display = '';
        btnReset.disabled = false;
        if (declFiles.length > 0) {
            invoiceInput.style.display = 'none';
            btnGenerate.style.display = 'none';
        } else {
            invoiceInput.style.display = '';
            btnGenerate.style.display = '';
            btnGenerate.disabled = false;
            btnGenerate.textContent = '生成报关单';
        }
    } else {
        // reset 等中间态
        invoiceInput.style.display = 'none';
        btnConfirm.style.display = 'none';
        btnGenerate.style.display = 'none';
        btnReset.style.display = 'none';
    }
}

/* ---------- 报关单文件列表 ---------- */
function renderFiles() {
    var box = document.getElementById('decl-files');
    if (!box) return;
    if (!declFiles.length) { box.style.display = 'none'; box.innerHTML = ''; return; }
    var html = '<div class="split-section-title">已生成报关单 <span class="count">' +
        declFiles.length + ' 个文件</span></div>';
    for (var i = 0; i < declFiles.length; i++) {
        var f = declFiles[i];
        var href = '/api/v1/split/' + encodeURIComponent(splitThreadId) +
            '/download/' + encodeURIComponent(f.name);
        var kb = f.size != null ? (Math.round(f.size / 102.4) / 10) + ' KB' : '';
        html += '<div style="padding:4px 0"><a href="' + esc(href) + '">' + esc(f.name) +
            '</a> <span class="muted" style="font-size:12px">' + esc(kb) + '</span></div>';
    }
    box.innerHTML = html;
    box.style.display = '';
}

function countTotalWarnings() {
    var total = 0;
    var ports = proposal.ports || [];
    for (var pi = 0; pi < ports.length; pi++) {
        var tickets = ports[pi].groups || [];
        for (var ti = 0; ti < tickets.length; ti++) {
            total += (tickets[ti].warnings || []).length;
        }
    }
    return total;
}

function updateWarningSummary(total) {
    var elements = [
        document.getElementById('warn-summary-top'),
        document.getElementById('warn-count-bottom')
    ];
    for (var i = 0; i < elements.length; i++) {
        var el = elements[i];
        if (!el) continue;
        if (total > 0) {
            el.textContent = '共 ' + total + ' 个警告';
            el.style.display = '';
        } else {
            el.style.display = 'none';
        }
    }
}

/* ---------- 校验 ---------- */
function validateTicket(ticket) {
    var w = [];
    // 保留引擎产出、前端无法重算的警告（如 non_sj_remainder 非商检剩余票）
    var old = ticket.warnings || [];
    for (var i = 0; i < old.length; i++) {
        if (old[i].rule !== 'over_3_full' && old[i].rule !== 'mixed_sj') {
            w.push(old[i]);
        }
    }
    if ((ticket.full_containers || 0) > 3) {
        w.push({ rule: 'over_3_full', message: '票内整柜超过 3 个：' + ticket.full_containers });
    }
    if ((ticket.sj_factories || []).length > 1) {
        w.push({ rule: 'mixed_sj', message: '票内含多种商检工厂：' + ticket.sj_factories.join('、') });
    }
    return w;
}

/* ---------- 拖拽 ---------- */
function handleDragStart(e) {
    if (!isEditable()) return;  // 已确认/已重置/已完成：只读禁拖
    var el = e.target.closest('[data-ticket-idx]');
    if (!el) return;

    var ticketIdx = parseInt(el.dataset.ticketIdx);
    var itemIdx = parseInt(el.dataset.itemIdx);
    if (isNaN(ticketIdx) || isNaN(itemIdx)) return;

    dragSrc = { ticketIdx: ticketIdx, itemIdx: itemIdx };
    el.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', ticketIdx + ':' + itemIdx);
}

function handleDragEnd(e) {
    var el = e.target.closest('[data-ticket-idx]');
    if (el) el.classList.remove('dragging');
    // 清除所有 drop-zone 高亮
    var zones = document.querySelectorAll('.ticket-card-body');
    for (var i = 0; i < zones.length; i++) zones[i].classList.remove('drop-zone');
    dragSrc = null;
}

function handleDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    var body = e.target.closest('.ticket-card-body');
    if (body) body.classList.add('drop-zone');
}

function handleDragLeave(e) {
    var body = e.target.closest('.ticket-card-body');
    if (body && !body.contains(e.relatedTarget)) {
        body.classList.remove('drop-zone');
    }
}

function handleDrop(e) {
    e.preventDefault();
    var bodies = document.querySelectorAll('.ticket-card-body');
    for (var i = 0; i < bodies.length; i++) bodies[i].classList.remove('drop-zone');

    if (!isEditable()) { dragSrc = null; return; }  // 只读状态禁拖
    if (!dragSrc) return;

    var card = e.target.closest('.ticket-card');
    if (!card) return;

    var targetTicketIdx = parseInt(card.dataset.ticketIdx);
    if (isNaN(targetTicketIdx)) return;

    var srcIdx = dragSrc.ticketIdx;
    if (srcIdx === targetTicketIdx) return; // 同一票，无变化

    // 在全局 tickets 列表中定位（data-ticket-idx 为原数组索引）
    var portData = getActivePortData();
    var allTickets = portData.groups || [];

    var srcTicket = allTickets[srcIdx];
    var tgtTicket = allTickets[targetTicketIdx];
    if (!srcTicket || !tgtTicket) { dragSrc = null; return; }

    // 跨港口检查（新建空票与当前票同港口，天然满足）
    if (srcTicket.port !== tgtTicket.port) {
        toast('不能跨港口拖动柜条目', 3000);
        dragSrc = null;
        return;
    }
    // 箱型检查：目标为空票（尚无箱型）时采用该柜箱型；
    // 否则一票一箱型，不一致拒绝
    if (tgtTicket.container_type) {
        if (srcTicket.container_type !== tgtTicket.container_type) {
            toast('不能跨箱型拖动柜条目（一票一箱型）', 3000);
            dragSrc = null;
            return;
        }
    } else {
        tgtTicket.container_type = srcTicket.container_type;
    }

    // 移动条目
    var items = srcTicket.items || [];
    if (dragSrc.itemIdx < 0 || dragSrc.itemIdx >= items.length) { dragSrc = null; return; }
    var movedItem = items.splice(dragSrc.itemIdx, 1)[0];
    if (!tgtTicket.items) tgtTicket.items = [];
    tgtTicket.items.push(movedItem);

    // 重算两张票的元数据
    recalcTicket(srcTicket);
    recalcTicket(tgtTicket);

    // 拖拽后票号按数组顺序重编（防序号漂移；拖出导致空票时序号仍连续）
    renumberTickets();

    dragSrc = null;
    render();
}

function recalcTicket(ticket) {
    var fullContainers = 0;
    var sjFactories = {};
    var items = ticket.items || [];

    // 票被拖空后回到「未指定箱型」状态，可再拖入任意箱型的第一个柜
    if (items.length === 0) {
        ticket.container_type = '';
    }

    for (var i = 0; i < items.length; i++) {
        var item = items[i];
        if (item.is_partial) {
            // 半票（商检半票 / 非商检剩余票）不计整柜
            if (item.factory_filter && isSJFactory(item.factory_filter)) {
                sjFactories[item.factory_filter] = true;
            }
        } else {
            fullContainers++;
        }
    }

    // 对全柜（factory_filter=null）的商检工厂，保留既有数据
    // 这里做简化处理：只追踪可明确归因的商检工厂（半票明确，全柜从旧值保留）
    var oldSJ = ticket.sj_factories || [];
    for (var j = 0; j < oldSJ.length; j++) {
        // 如果旧 SJ 工厂在某个全柜中且该柜还在，保留
        sjFactories[oldSJ[j]] = true;
    }

    ticket.full_containers = fullContainers;
    ticket.sj_factories = Object.keys(sjFactories).sort();

    // 重新校验
    ticket.warnings = validateTicket(ticket);
}

/* ---------- 确认 / 强制确认弹窗 ---------- */
function confirmSplit() {
    var total = countTotalWarnings();
    if (total > 0) {
        showForceConfirmDialog(total);
    } else {
        confirmSplitAction(false);
    }
}

function showForceConfirmDialog(warnCount) {
    document.getElementById('force-msg').textContent =
        '该方案存在 ' + warnCount + ' 个警告，强制通过后仍需人工核查，确定提交？';
    document.getElementById('force-modal').classList.add('show');
}

function closeForceModal() {
    document.getElementById('force-modal').classList.remove('show');
}

function forceConfirm() {
    closeForceModal();
    confirmSplitAction(true);
}

function resetSplit() {
    if (!confirm('确定要重置分票方案？当前方案将被清除，需重新运行分票引擎。')) return;
    resetSplitAction();
}

/* ---------- 错误处理 ---------- */
function showError(msg) {
    var layout = document.getElementById('split-layout');
    if (layout) {
        layout.innerHTML =
            '<div class="card" style="text-align:center;padding:40px">' +
            '<span style="color:var(--err);font-size:15px">' + esc(msg) + '</span>' +
            '<br><a class="btn" href="/" style="margin-top:16px">&larr; 返回工作台</a></div>';
    }
    var bar = document.getElementById('action-bar');
    if (bar) bar.style.display = 'none';
}

/* ---------- 键盘：Esc 关闭弹窗 ---------- */
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        var modal = document.getElementById('force-modal');
        if (modal && modal.classList.contains('show')) closeForceModal();
    }
});

/* ---------- 启动 ---------- */
init();