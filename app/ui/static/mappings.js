"use strict";

/* ============================================================
   主数据维护页 — 前端交互逻辑
   依赖 ui.js 的 $ / esc / toast / api / renderTopbar
   ============================================================ */

/* ---------- 全局状态 ---------- */
var activeTab = "products";       // products | groups | factories | skus
var products = [];                // 产品映射列表缓存
var groups = [];                  // 品名组列表缓存
var factories = [];               // 工厂列表缓存（含别名）
var skus = [];                    // SKU 主数据列表缓存
var editingProductId = null;      // null=新增
var editingGroupId = null;        // null=新增
var editingFactoryId = null;      // null=新增
var editingSkuId = null;          // SKU 主数据仅编辑

/* ---------- 初始化 ---------- */
function init() {
    renderTopbar("mappings");  // 链接表已含主数据维护，直接高亮
    loadProducts();
    loadGroups();
    loadFactories();
}

/* ---------- Tab 切换 ---------- */
function switchTab(tab) {
    activeTab = tab;
    var tabs = ["products", "groups", "factories", "skus"];
    tabs.forEach(function (t) {
        document.getElementById("tab-" + t).className = "port-tab" + (tab === t ? " active" : "");
        document.getElementById("pane-" + t).style.display = tab === t ? "" : "none";
    });
    if (tab === "groups") loadGroups();
    if (tab === "factories") loadFactories();
    if (tab === "skus") loadSkus();
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

/* ============================================================
   工厂与别名
   ============================================================ */

async function loadFactories() {
    try {
        factories = await api("/api/v1/mappings/factories");
        renderFactories();
        renderSkuFactoryOptions();
    } catch (e) {
        toast("工厂列表加载失败：" + e.message, 4000);
        document.getElementById("factory-list").innerHTML =
            '<p class="empty-hint">加载失败</p>';
    }
}

function findFactory(id) {
    return factories.find(function (x) { return x.id === id; });
}

function renderFactories() {
    var el = document.getElementById("factory-list");
    document.getElementById("factory-count").textContent =
        factories && factories.length ? "共 " + factories.length + " 家工厂" : "";
    if (!factories || factories.length === 0) {
        el.innerHTML = '<p class="empty-hint">暂无工厂，点右上角「新增工厂」创建</p>';
        return;
    }
    el.innerHTML = factories.map(function (f) {
        var shortHtml = f.short_name
            ? '<span class="factory-shortname">短名：' + esc(f.short_name) + "</span>"
            : '<span class="factory-shortname-missing">短名未设置</span>';
        var aliases = f.aliases || [];
        var aliasRows = aliases.map(function (a) {
            return '<tr class="alias-row">'
                + '<td><input type="text" value="' + esc(a.alias) + '" id="alias-text-' + a.id + '"'
                + ' onchange="saveAlias(' + a.id + ')" style="width:100%"></td>'
                + '<td style="white-space:nowrap">'
                + '<label class="alias-use-check"><input type="checkbox" id="alias-folder-' + a.id + '"'
                + (a.use_folder_match ? " checked" : "") + ' onchange="saveAlias(' + a.id + ')"> 文件夹匹配</label>'
                + '<label class="alias-use-check"><input type="checkbox" id="alias-excel-' + a.id + '"'
                + (a.use_excel_normalize ? " checked" : "") + ' onchange="saveAlias(' + a.id + ')"> Excel 归一</label>'
                + "</td>"
                + '<td class="col-actions"><button class="btn btn-sm" onclick="deleteAlias(' + a.id + ')">删除</button></td>'
                + "</tr>";
        }).join("");
        var aliasTable = aliases.length
            ? '<table class="table"><thead><tr><th>别名</th><th>用途</th><th>操作</th></tr></thead><tbody>'
              + aliasRows + "</tbody></table>"
            : '<p class="empty-hint" style="padding:8px 0">暂无别名</p>';
        return '<div class="card">'
            + '<div class="group-card-header">'
            + "<strong>" + esc(f.factory_name) + "</strong>"
            + shortHtml
            + '<label class="toggle-label">商检工厂 '
            + '<span class="toggle"><input type="checkbox"' + (f.is_inspection_factory ? " checked" : "")
            + ' onchange="toggleInspection(' + f.id + ", this)\"><span class=\"slider\"></span></span>"
            + "</label>"
            + '<span class="muted">SKU ' + (f.sku_count || 0) + " 条</span>"
            + '<span class="spacer"></span>'
            + '<button class="btn btn-sm" onclick="openFactoryModal(' + f.id + ')">编辑</button>'
            + '<button class="btn btn-sm" onclick="deleteFactory(' + f.id + ')">删除</button>'
            + "</div>"
            + aliasTable
            + '<div class="alias-add-row">'
            + '<input type="text" id="new-alias-' + f.id + '" placeholder="新别名（日文名 / 全称 / Excel 变体）">'
            + '<label class="alias-use-check"><input type="checkbox" id="new-alias-folder-' + f.id + '" checked> 文件夹匹配</label>'
            + '<label class="alias-use-check"><input type="checkbox" id="new-alias-excel-' + f.id + '"> Excel 归一</label>'
            + '<button class="btn btn-sm" onclick="addAlias(' + f.id + ')">加别名</button>'
            + "</div>"
            + "</div>";
    }).join("");
}

function openFactoryModal(id) {
    editingFactoryId = id;
    var f = id ? findFactory(id) : null;
    document.getElementById("fm-title").textContent = f ? "编辑工厂" : "新增工厂";
    document.getElementById("fm-name").value = f ? (f.factory_name || "") : "";
    document.getElementById("fm-short").value = f ? (f.short_name || "") : "";
    document.getElementById("fm-inspection").checked = f ? !!f.is_inspection_factory : false;
    document.getElementById("factory-modal").classList.add("show");
}

function closeFactoryModal() {
    document.getElementById("factory-modal").classList.remove("show");
    editingFactoryId = null;
}

async function saveFactory() {
    var name = document.getElementById("fm-name").value.trim();
    if (!name) {
        toast("请填写工厂规范名");
        document.getElementById("fm-name").focus();
        return;
    }
    var inspection = document.getElementById("fm-inspection").checked;
    var old = editingFactoryId ? findFactory(editingFactoryId) : null;
    if (old && !!old.is_inspection_factory !== inspection) {
        if (!confirm("商检工厂标记影响后续批次分票判定，确认修改？")) return;
    }
    var body = {
        factory_name: name,
        short_name: document.getElementById("fm-short").value.trim() || null,
        is_inspection_factory: inspection,
    };
    var btn = document.getElementById("fm-save");
    btn.disabled = true;
    btn.textContent = "保存中…";
    try {
        if (editingFactoryId) {
            await api("/api/v1/mappings/factories/" + editingFactoryId, {
                method: "PUT", body: body
            });
        } else {
            await api("/api/v1/mappings/factories", {
                method: "POST", body: body
            });
        }
        toast("已保存");
        closeFactoryModal();
        loadFactories();
    } catch (e) {
        toast("保存失败：" + e.message, 4000);
    } finally {
        btn.disabled = false;
        btn.textContent = "保存";
    }
}

async function deleteFactory(id) {
    var f = findFactory(id);
    var label = f ? f.factory_name : ("id=" + id);
    if (!confirm("确定删除工厂「" + label + "」？有 SKU 或别名关联时会被拒绝。")) return;
    try {
        await api("/api/v1/mappings/factories/" + id, { method: "DELETE" });
        toast("已删除");
        loadFactories();
    } catch (e) {
        toast("删除失败：" + e.message, 4000);
    }
}

/* 商检工厂开关：改动前确认（影响后续批次分票判定），取消则还原 */
async function toggleInspection(id, checkbox) {
    var f = findFactory(id);
    if (!f) return;
    var next = checkbox.checked;
    if (!confirm("商检工厂标记影响后续批次分票判定，确认" + (next ? "开启" : "关闭") + "「" + f.factory_name + "」的商检标记？")) {
        checkbox.checked = !next;
        return;
    }
    try {
        await api("/api/v1/mappings/factories/" + id, {
            method: "PUT",
            body: {
                factory_name: f.factory_name,
                short_name: f.short_name || null,
                is_inspection_factory: next,
            }
        });
        toast("已" + (next ? "开启" : "关闭") + "商检工厂标记");
        loadFactories();
    } catch (e) {
        checkbox.checked = !next;
        toast("修改失败：" + e.message, 4000);
    }
}

async function addAlias(factoryId) {
    var input = document.getElementById("new-alias-" + factoryId);
    var alias = input.value.trim();
    if (!alias) {
        toast("请填写别名");
        input.focus();
        return;
    }
    var body = {
        alias: alias,
        use_folder_match: document.getElementById("new-alias-folder-" + factoryId).checked,
        use_excel_normalize: document.getElementById("new-alias-excel-" + factoryId).checked,
    };
    if (!body.use_folder_match && !body.use_excel_normalize) {
        toast("文件夹匹配 / Excel 归一至少勾选一个用途");
        return;
    }
    try {
        await api("/api/v1/mappings/factories/" + factoryId + "/aliases", {
            method: "POST", body: body
        });
        toast("已添加别名");
        loadFactories();
    } catch (e) {
        toast("添加失败：" + e.message, 4000);
    }
}

/* 别名行内编辑：文本或任一用途勾选变化时整体提交 */
async function saveAlias(aliasId) {
    var body = {
        alias: document.getElementById("alias-text-" + aliasId).value.trim(),
        use_folder_match: document.getElementById("alias-folder-" + aliasId).checked,
        use_excel_normalize: document.getElementById("alias-excel-" + aliasId).checked,
    };
    if (!body.alias) {
        toast("别名不能为空");
        loadFactories();
        return;
    }
    if (!body.use_folder_match && !body.use_excel_normalize) {
        toast("文件夹匹配 / Excel 归一至少勾选一个用途");
        loadFactories();
        return;
    }
    try {
        await api("/api/v1/mappings/aliases/" + aliasId, {
            method: "PUT", body: body
        });
        toast("别名已保存");
        loadFactories();
    } catch (e) {
        toast("保存失败：" + e.message, 4000);
        loadFactories();
    }
}

async function deleteAlias(aliasId) {
    if (!confirm("确定删除该别名？删除后文件夹匹配 / Excel 归一化将不再识别它。")) return;
    try {
        await api("/api/v1/mappings/aliases/" + aliasId, { method: "DELETE" });
        toast("已删除");
        loadFactories();
    } catch (e) {
        toast("删除失败：" + e.message, 4000);
    }
}

/* ============================================================
   SKU 主数据
   ============================================================ */

/* 工厂下拉筛选选项（随工厂列表刷新，保留当前选中） */
function renderSkuFactoryOptions() {
    var sel = document.getElementById("s-factory");
    if (!sel) return;
    var current = sel.value;
    var html = '<option value="">全部工厂</option>' + (factories || []).map(function (f) {
        return '<option value="' + f.id + '">' + esc(f.short_name || f.factory_name) + "</option>";
    }).join("");
    sel.innerHTML = html;
    sel.value = current;
}

async function loadSkus() {
    var factoryId = document.getElementById("s-factory").value;
    var q = document.getElementById("s-q").value.trim();
    var url = "/api/v1/mappings/skus?";
    if (factoryId) url += "factory_id=" + encodeURIComponent(factoryId) + "&";
    if (q) url += "q=" + encodeURIComponent(q);
    try {
        skus = await api(url);
        renderSkus();
    } catch (e) {
        toast("SKU 主数据加载失败：" + e.message, 4000);
        document.getElementById("sku-tbody").innerHTML =
            '<tr><td colspan="8" class="empty">加载失败</td></tr>';
    }
}

function renderSkus() {
    var tbody = document.getElementById("sku-tbody");
    document.getElementById("sku-count").textContent =
        skus && skus.length ? "共 " + skus.length + " 条" : "";
    if (!skus || skus.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="empty">暂无数据</td></tr>';
        return;
    }
    var dash = function (v) { return (v !== null && v !== undefined && v !== "") ? esc(v) : '<span class="muted">-</span>'; };
    tbody.innerHTML = skus.map(function (k) {
        var sj = k.inspection_required ? '<span class="sj-mark">✓</span>' : "";
        return "<tr>"
            + '<td class="mono"><strong>' + esc(k.sku_code) + "</strong></td>"
            + "<td>" + dash(k.name_cn) + "</td>"
            + "<td>" + dash(k.name_en) + "</td>"
            + '<td class="mono">' + dash(k.hs_code) + "</td>"
            + "<td>" + sj + "</td>"
            + "<td>" + dash(k.unit_net_weight) + "</td>"
            + "<td>" + dash(k.unit_gross_weight) + "</td>"
            + '<td class="col-actions"><button class="btn btn-sm" onclick="openSkuModal(' + k.sku_id + ')">编辑</button></td>'
            + "</tr>";
    }).join("");
}

function openSkuModal(skuId) {
    editingSkuId = skuId;
    var k = skus.find(function (x) { return x.sku_id === skuId; });
    if (!k) return;
    document.getElementById("sm-title").textContent = "编辑 SKU 主数据";
    document.getElementById("sm-code").value = k.sku_code || "";
    document.getElementById("sm-name-cn").value = k.name_cn || "";
    document.getElementById("sm-name-en").value = k.name_en || "";
    document.getElementById("sm-hs").value = k.hs_code || "";
    document.getElementById("sm-inspection").checked = !!k.inspection_required;
    document.getElementById("sm-net").value = k.unit_net_weight != null ? String(k.unit_net_weight) : "";
    document.getElementById("sm-gross").value = k.unit_gross_weight != null ? String(k.unit_gross_weight) : "";
    document.getElementById("sku-modal").classList.add("show");
}

function closeSkuModal() {
    document.getElementById("sku-modal").classList.remove("show");
    editingSkuId = null;
}

/* 解析重量输入：留空 → null（下批次 Node4 重算）；非数字报错 */
function parseWeight(inputId, label) {
    var raw = document.getElementById(inputId).value.trim();
    if (!raw) return null;
    var v = parseFloat(raw);
    if (isNaN(v) || v < 0) {
        throw new Error(label + "必须是非负数字，或留空");
    }
    return v;
}

async function saveSku() {
    if (!editingSkuId) return;
    var net, gross;
    try {
        net = parseWeight("sm-net", "单件净重");
        gross = parseWeight("sm-gross", "单件毛重");
    } catch (e) {
        toast(e.message, 3500);
        return;
    }
    var body = {
        name_cn: document.getElementById("sm-name-cn").value.trim() || null,
        name_en: document.getElementById("sm-name-en").value.trim() || null,
        hs_code: document.getElementById("sm-hs").value.trim() || null,
        inspection_required: document.getElementById("sm-inspection").checked,
        unit_net_weight: net,
        unit_gross_weight: gross,
    };
    var btn = document.getElementById("sm-save");
    btn.disabled = true;
    btn.textContent = "保存中…";
    try {
        var result = await api("/api/v1/mappings/skus/" + editingSkuId, {
            method: "PUT", body: body
        });
        var n = result && result.audited_fields ? result.audited_fields.length : 0;
        toast(n > 0 ? "已保存，已记录留痕（" + n + " 个字段变更）" : "已保存（无字段变化）", 3500);
        closeSkuModal();
        loadSkus();
    } catch (e) {
        toast("保存失败：" + e.message, 4000);
    } finally {
        btn.disabled = false;
        btn.textContent = "保存";
    }
}

/* ---------- 键盘：Esc 关闭弹窗 ---------- */
document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    if (document.getElementById("product-modal").classList.contains("show")) closeProductModal();
    if (document.getElementById("group-modal").classList.contains("show")) closeGroupModal();
    if (document.getElementById("factory-modal").classList.contains("show")) closeFactoryModal();
    if (document.getElementById("sku-modal").classList.contains("show")) closeSkuModal();
});

/* ---------- 启动 ---------- */
init();
