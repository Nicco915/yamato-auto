# 主数据修复日志

- **日期**: 2026-08-24
- **Git 仓库**: `/Users/nz/downloads/yamato/app`
- **分支**: `hotfix/20260824-master-data-recovery`
- **涉及数据库**: `app/app/data/master.db`

## 事故现象

测试未做 `YAMATO_DOTENV_PATH` 隔离，导致生产 `master.db` 被清空部分主数据表。修复前检查到的记录数：

| 表 | 修复前 | 正常预期 |
|---|---|---|
| `factories` | 5（short_name 全为空） | 10 |
| `factory_aliases` | 0 | 12 |
| `factory_skus` | 131 | 131 |
| `product_mappings` | 0 | 37 |
| `product_groups` | 0 | 3 |
| `product_group_members` | 0 | 11 |

## 恢复来源

由于当前仓库是 Git 仓库，但 `app/data/` 已被 `.gitignore` 排除，数据库本身无法通过 Git 恢复。本次恢复依赖以下代码/配置文件作为“种子”：

1. `app/alias_map.json` → 工厂别名（文件夹匹配）
2. `app/config.py` 中的 `FACTORY_NORMALIZE_MAP` / `INSPECTION_FACTORIES` → Excel 归一化 / 商检标记
3. `scripts/import_product_mappings.py` 中的 `GROUPS` → 3 组品名组
4. `96/报关匹配东京.xlsx` → 37 条产品映射

## 执行步骤

### 1. 工厂别名迁移

```bash
python3 scripts/migrate_factory_aliases.py
```

结果：
- 新建工厂 7 家
- 回填 short_name 5 家
- 新增别名 12 条（含 2 条双用途合并）

### 2. 产品映射 + 品名组恢复

```bash
YAMATO_ALLOW_DESTRUCTIVE=1 python3 scripts/remediate_master_data.py
```

该脚本会先创建快照，然后：
- 导入产品映射 37 条
- 恢复品名组 3 组（成员 11 个）
- 清理无关联的孤儿工厂 2 个

### 3. 验证

数据库记录数：

```text
factories               : 10
factory_aliases         : 12
factory_skus            : 131
product_mappings        : 37
product_groups          : 3
product_group_members   : 11
```

相关测试全部通过：

```bash
python3 -m pytest app/tests/test_factory_match_db.py app/tests/test_declare.py -v
# 27 passed, 1 warning
```

## 快照留存

修复前后共生成 3 份快照（按时间顺序）：

- `app/data/backups/20260824_093115/master_pre_remediation.db`
- `app/data/backups/20260824_093115/master_post_alias_migration.db`
- `app/data/backups/20260824_094841/master.db`

每份快照均包含 `master.db`、`checkpoints.db`、`alias_map.json`、`.env`。

## 无法自动恢复的数据

以下数据如果在事故前通过 `/mappings` 页人工增改过，本次无法恢复：

- `alias_map.json` 之外的额外工厂别名
- `product_mappings` 中手工填写的 `factory_id`、`sku_code`
- 除脚本内置 3 组之外的额外品名组
- 在 UI 中修改过的 SKU 单重/品名等

如有需要，请在 `/mappings` 页人工补录。

## 后续备份方案

1. **写前快照**：所有会改库的脚本（迁移、导入、修复）先调用 `scripts/backup_master.py`。
2. **确认门**：人工执行改库脚本时必须输入 `yes` 或设置 `YAMATO_ALLOW_DESTRUCTIVE=1`。
3. **定时备份**：每天启动 uvicorn 或 macOS `launchd` 调用一次 `backup_master.py`，保留最近 30 份。
4. **种子文件化**：把 `factory_aliases`、`product_groups`、`product_mappings` 定期导出为 JSON 种子并提交到 Git。
5. **测试隔离**：所有子进程测试必须设置 `YAMATO_DOTENV_PATH` 指向临时 `.env`，禁止直连生产库。

---

# 品名映射 SKU 补充日志

- **日期**: 2026-08-24
- **分支**: `main`
- **脚本**: `scripts/supplement_sku_mappings.py`
- **涉及数据库**: `app/app/data/master.db`

## 补充规则（与用户确认）

1. 只补充当前 `product_mappings` 中**已存在**的品名；品名不在当前映射中的，全部跳过。
2. 对每个已存在品名，删除 `sku_code` 为 `NULL` 的原品名级兜底行。
3. 按用户提供的 SKU 清单，为每个 SKU 插入一行 SKU 级映射，复制原行的 `hs_code` / `supplier_name` / `inspection_required` / `name_en` / `unit_code` / `factory_id`。
4. 已存在于任意品名下的 SKU 跳过，避免重复。

## 处理结果

```text
已处理品名: 8
  木橱: 删除 NULL 行 1，新增 SKU 行 1
  木箱: 删除 NULL 行 1，新增 SKU 行 6
  木盖: 删除 NULL 行 1，新增 SKU 行 4
  44木架: 删除 NULL 行 1，新增 SKU 行 15
  木制抓挠盒: 删除 NULL 行 1，新增 SKU 行 1
  写字板: 删除 NULL 行 1，新增 SKU 行 3
  坐垫: 删除 NULL 行 1，新增 SKU 行 11
  枕头: 删除 NULL 行 1，新增 SKU 行 3
```

- **product_mappings 总数**: `37 → 73`
- **跳过的品名**（当前映射中不存在）: 共 20 个，例如 `松木木制凳子`、`桐木木被架`、`6件套`、`3件套` 等。
- **跳过的 SKU**（已存在）: 无。

## 快照

本次补充前快照：

```text
app/data/backups/20260824_165007/
├── master.db
├── checkpoints.db
├── alias_map.json
└── .env
```

## 跳过的近似品名

以下品名在当前映射中无完全匹配，但被识别为可能对应短名，已按用户要求跳过：

| 用户提供的品名 | 可能的当前映射 |
|---|---|
| 松木木制凳子 | 木制凳子 |
| 松木杉木木被架 | 木被架 |
| 桐木橱柜搁架 | 木橱 / 橱柜搁架 |
| 桐木被架床 | 木被架 / 被架床 |
| 桐木木制菜板 | 木制菜板 |
| 桐木木被架 | 木被架 |
| 杉木被架床 | 木被架 / 被架床 |
| 松木木筐 | 木筐 |
| 松木杨木木制抓挠盒 | 木制抓挠盒 |
| 桧木木被架 | 木被架 |

如需把这些近似品名也关联到当前短名，请告诉我，我再执行一轮补充。

