"use strict";

/* ============================================================
   主数据维护页 — 前端交互逻辑
   依赖 ui.js 的 $ / esc / toast / api / renderTopbar
   ============================================================ */

/* ---------- 全局状态 ---------- */
var activeTab = "products";       // products | groups
var products = [];                // 产品映射列表缓存
var groups = [];                  // 品名组列表缓存
var editingProductId = null;      // null=新增
var editingGroupId = null;        // null=新增

/* ---------- 初始化 ---------- */
function init() {
    // 顶栏：注入「主数据维护」导航链接并高亮（ui.js 链接表固定，这里追加）
    var bar = renderTopbar("");
    var nav = bar.querySelector(".nav");
    if (nav) {
        nav.insertAdjacentHTML("beforeend", '<a href="/mappings" class="active">主数据维护</a>');
    }
    loadProducts();
    loadGroups();
}

/* ---------- Tab 切换 ---------- */
function switchTab(tab) {
    activeTab = tab;
    document.getElementById("tab-products").className = "port-tab" + (tab === "products" ? " active" : "");
    document.getElementById("tab-groups").className = "port-tab" + (tab === "groups" ? " active" : "");
    document.getElementById("pane-products").style.display = tab === "products" ? "" : "none";
    document.getElementById("pane-groups").style.display = tab === "groups" ? "" : "none";
    if (tab === "groups") loadGroups();
}

/* ============================================================
   产品映射
   ============================================================ */

async function loadProducts() {
    var q = document.getElementById("p-q").value.trim();
    var incomplete = document.getElementById("p-incomplete").checked;
    var url = "/api/v1/mappings/products?";
    if (q) url += "q=" + encodeURIComponent(q) + "&";
    if (incomplete) url += "incomplete=true";
    try {
        products = await api(url);
        renderProducts();
    } catch (e) {
        toast("映射列表加载失败：" + e.message, 4000);
        document.getElementById("product-tbody").innerHTML =
            '<tr><td colspan="8" class="empty">加载失败</td></tr>';
    }
}

function renderProducts() {
    var tbody = document.getElementById("product-tbody");
    if (!products || products.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="empty">暂无数据</td></tr>';
        return;
    }
    tbody.innerHTML = products.map(function (m) {
        var rowCls = m.is_incomplete ? ' class="row-incomplete"' : "";
        var sj = m.inspection_required ? '<span class="sj-mark">✓</span>' : "";
        var dash = function (v) { return v ? esc(v) : '<span class="muted">-</span>'; };
        return "<tr" + rowCls + ">"
            + "<td><strong>" + esc(m.product_name_cn) + "</strong></td>"
            + "<td class=\"mono\">" + dash(m.hs_code) + "</td>"
            + "<td>" + dash(m.supplier_name) + "</td>"
            + "<td>" + sj + "</td>"
            + "<td>" + dash(m.name_en) + "</td>"
            + "<td>" + dash(m.unit_code) + "</td>"
            + "<td>" + dash(m.sku_code) + "</td>"
            + '<td class="col-actions">'
            + '<button class="btn btn-sm" onclick="openProductModal(' + m.id + ')">编辑</button>'
            + '<button class="btn btn-sm" onclick="deleteProduct(' + m.id + ')">删除</button>'
            + "</td></tr>";
    }).join("");
}

function openProductModal(id) {
    editingProductId = id;
    var m = id ? products.find(function (x) { return x.id === id; }) : null;
    document.getElementById("pm-title").textContent = m ? "编辑映射" : "新增映射";
    document.getElementById("pm-name").value = m ? (m.product_name_cn || "") : "";
    document.getElementById("pm-hs").value = m ? (m.hs_code || "") : "";
    document.getElementById("pm-supplier").value = m ? (m.supplier_name || "") : "";
    document.getElementById("pm-inspection").checked = m ? !!m.inspection_required : false;
    document.getElementById("pm-name-en").value = m ? (m.name_en || "") : "";
    document.getElementById("pm-unit").value = m ? (m.unit_code || "") : "";
    document.getElementById("pm-sku").value = m ? (m.sku_code || "") : "";
    document.getElementById("pm-factory").value = m && m.factory_id != null ? String(m.factory_id) : "";
    document.getElementById("product-modal").classList.add("show");
}

function closeProductModal() {
    document.getElementById("product-modal").classList.remove("show");
    editingProductId = null;
}

async function saveProduct() {
    var name = document.getElementById("pm-name").value.trim();
    if (!name) {
        toast("请填写中文品名");
        document.getElementById("pm-name").focus();
        return;
    }
    var factoryRaw = document.getElementById("pm-factory").value.trim();
    var factoryId = null;
    if (factoryRaw) {
        factoryId = parseInt(factoryRaw, 10);
        if (isNaN(factoryId)) {
            toast("工厂 ID 必须是数字");
            return;
        }
    }
    var body = {
        product_name_cn: name,
        hs_code: document.getElementById("pm-hs").value.trim() || null,
        supplier_name: document.getElementById("pm-supplier").value.trim() || null,
        inspection_required: document.getElementById("pm-inspection").checked,
        name_en: document.getElementById("pm-name-en").value.trim() || null,
        unit_code: document.getElementById("pm-unit").value.trim() || null,
        sku_code: document.getElementById("pm-sku").value.trim() || null,
        factory_id: factoryId,
    };
    var btn = document.getElementById("pm-save");
    btn.disabled = true;
    btn.textContent = "保存中…";
    try {
        var result;
        if (editingProductId) {
            result = await api("/api/v1/mappings/products/" + editingProductId, {
                method: "PUT", body: body
            });
        } else {
            result = await api("/api/v1/mappings/products", {
                method: "POST", body: body
            });
        }
        var msg = "已保存";
        if (result && result.synced_skus) {
            msg += "，已回填 " + result.synced_skus + " 条 SKU 主数据";
        }
        toast(msg);
        closeProductModal();
        loadProducts();
    } catch (e) {
        toast("保存失败：" + e.message, 4000);
    } finally {
        btn.disabled = false;
        btn.textContent = "保存";
    }
}

async function deleteProduct(id) {
    var m = products.find(function (x) { return x.id === id; });
    var label = m ? m.product_name_cn : ("id=" + id);
    if (!confirm("确定删除映射「" + label + "」？此操作不可恢复。")) return;
    try {
        await api("/api/v1/mappings/products/" + id, { method: "DELETE" });
        toast("已删除");
        loadProducts();
    } catch (e) {
        toast("删除失败：" + e.message, 4000);
    }
}

/* Excel 批量导入（.xlsx，列：产品|税号|供应商|商检|产品组一|自定义七） */
async function importProducts(input) {
    var file = input.files && input.files[0];
    input.value = "";  // 允许重复选同一文件
    if (!file) return;
    if (!confirm("将从「" + file.name + "」导入产品映射（按品名+供应商去重，已有记录会更新）。继续？")) return;
    var fd = new FormData();
    fd.append("file", file);
    try {
        var resp = await fetch("/api/v1/mappings/products/import", {
            method: "POST", body: fd
        });
        var data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || ("HTTP " + resp.status));
        toast("导入完成：新增 " + data.created + " 条，更新 " + data.updated + " 条", 4000);
        loadProducts();
    } catch (e) {
        toast("导入失败：" + e.message, 4000);
    }
}

/* ============================================================
   品名组
   ============================================================ */

async function loadGroups() {
    try {
        groups = await api("/api/v1/mappings/groups");
        renderGroups();
    } catch (e) {
        toast("品名组加载失败：" + e.message, 4000);
        document.getElementById("group-list").innerHTML =
            '<p class="empty-hint">加载失败</p>';
    }
}

function groupTypeTag(t) {
    if (t === "set_split") return '<span class="group-tag tag-set">组套拆分</span>';
    if (t === "box_share") return '<span class="group-tag tag-box">配对均分</span>';
    return '<span class="group-tag">' + esc(t) + "</span>";
}

function renderGroups() {
    var el = document.getElementById("group-list");
    document.getElementById("group-count").textContent =
        groups && groups.length ? "共 " + groups.length + " 组" : "";
    if (!groups || groups.length === 0) {
        el.innerHTML = '<p class="empty-hint">暂无品名组，点右上角「新增组」创建</p>';
        return;
    }
    el.innerHTML = groups.map(function (g) {
        var members = g.members || [];
        var rowsHtml = members.map(function (mb, i) {
            return "<tr>"
                + "<td>" + (mb.display_order != null ? mb.display_order : i) + "</td>"
                + "<td>" + esc(mb.product_name_cn) + "</td>"
                + "<td>" + (mb.split_price != null ? esc(mb.split_price) : '<span class="muted">-</span>') + "</td>"
                + "<td>" + (mb.split_net_weight != null ? esc(mb.split_net_weight) : '<span class="muted">-</span>') + "</td>"
                + "</tr>";
        }).join("");
        return '<div class="card group-card">'
            + '<div class="group-card-header">'
            + "<strong>" + esc(g.name) + "</strong>"
            + groupTypeTag(g.group_type)
            + '<span class="muted">源品名：' + esc(g.source_name_cn) + "</span>"
            + '<span class="spacer"></span>'
            + '<button class="btn btn-sm" onclick="openGroupModal(' + g.id + ')">编辑</button>'
            + '<button class="btn btn-sm" onclick="deleteGroup(' + g.id + ')">删除</button>'
            + "</div>"
            + '<table class="table">'
            + "<thead><tr><th>顺序</th><th>品名</th><th>单价</th><th>单重</th></tr></thead>"
            + "<tbody>" + rowsHtml + "</tbody></table>"
            + "</div>";
    }).join("");
}

function openGroupModal(id) {
    editingGroupId = id;
    var g = id ? groups.find(function (x) { return x.id === id; }) : null;
    document.getElementById("gm-title").textContent = g ? "编辑品名组" : "新增品名组";
    document.getElementById("gm-name").value = g ? (g.name || "") : "";
    document.getElementById("gm-type").value = g ? (g.group_type || "set_split") : "set_split";
    document.getElementById("gm-source").value = g ? (g.source_name_cn || "") : "";
    var lines = "";
    if (g) {
        lines = (g.members || []).map(function (mb) {
            return (mb.product_name_cn || "") + ","
                + (mb.split_price != null ? mb.split_price : "") + ","
                + (mb.split_net_weight != null ? mb.split_net_weight : "");
        }).join("\n");
    }
    document.getElementById("gm-members").value = lines;
    document.getElementById("group-modal").classList.add("show");
}

function closeGroupModal() {
    document.getElementById("group-modal").classList.remove("show");
    editingGroupId = null;
}

/* 解析成员文本域：每行 品名,单价,单重（逗号兼容中英文） */
function parseMembers(text) {
    var lines = text.split("\n");
    var members = [];
    for (var i = 0; i < lines.length; i++) {
        var line = lines[i].trim();
        if (!line) continue;
        var parts = line.split(/[,，]/);
        var name = (parts[0] || "").trim();
        if (!name) {
            throw new Error("第 " + (i + 1) + " 行缺少品名");
        }
        var price = (parts[1] || "").trim();
        var weight = (parts[2] || "").trim();
        var mb = {
            product_name_cn: name,
            display_order: members.length,
            split_price: price ? parseFloat(price) : null,
            split_net_weight: weight ? parseFloat(weight) : null,
        };
        if (price && isNaN(mb.split_price)) {
            throw new Error("第 " + (i + 1) + " 行单价不是数字：" + price);
        }
        if (weight && isNaN(mb.split_net_weight)) {
            throw new Error("第 " + (i + 1) + " 行单重不是数字：" + weight);
        }
        members.push(mb);
    }
    if (members.length === 0) {
        throw new Error("至少填写一个成员");
    }
    return members;
}

async function saveGroup() {
    var name = document.getElementById("gm-name").value.trim();
    var source = document.getElementById("gm-source").value.trim();
    if (!name) { toast("请填写组名"); return; }
    if (!source) { toast("请填写源品名"); return; }
    var members;
    try {
        members = parseMembers(document.getElementById("gm-members").value);
    } catch (e) {
        toast(e.message, 3500);
        return;
    }
    var body = {
        name: name,
        group_type: document.getElementById("gm-type").value,
        source_name_cn: source,
        members: members,
    };
    var btn = document.getElementById("gm-save");
    btn.disabled = true;
    btn.textContent = "保存中…";
    try {
        if (editingGroupId) {
            await api("/api/v1/mappings/groups/" + editingGroupId, {
                method: "PUT", body: body
            });
        } else {
            await api("/api/v1/mappings/groups", {
                method: "POST", body: body
            });
        }
        toast("已保存");
        closeGroupModal();
        loadGroups();
    } catch (e) {
        toast("保存失败：" + e.message, 4000);
    } finally {
        btn.disabled = false;
        btn.textContent = "保存";
    }
}

async function deleteGroup(id) {
    var g = groups.find(function (x) { return x.id === id; });
    var label = g ? g.name : ("id=" + id);
    if (!confirm("确定删除品名组「" + label + "」？组内成员将一并删除。")) return;
    try {
        await api("/api/v1/mappings/groups/" + id, { method: "DELETE" });
        toast("已删除");
        loadGroups();
    } catch (e) {
        toast("删除失败：" + e.message, 4000);
    }
}

/* ---------- 键盘：Esc 关闭弹窗 ---------- */
document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    if (document.getElementById("product-modal").classList.contains("show")) closeProductModal();
    if (document.getElementById("group-modal").classList.contains("show")) closeGroupModal();
});

/* ---------- 启动 ---------- */
init();
