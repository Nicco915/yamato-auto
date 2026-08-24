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
