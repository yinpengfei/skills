---
name: dbq
description: |-
  Multi-database SQL assistant — query MySQL, PostgreSQL, SQLite & MariaDB
  across dev/test/prod environments with a single command.
  Security-first: read-only by default, DML/DDL gating, EXPLAIN analysis.
  多数据库查询助手，一个命令搞定 MySQL/PostgreSQL/SQLite/MariaDB 和多套环境。
agent_created: true
---

# 数据库操作技能 (dbq)

多环境数据库 CLI，默认只读，写操作需 YAML 配置显式开启。支持 MySQL / PostgreSQL / SQLite / MariaDB。

## 快速配置

```bash
python scripts/query.py --init-config     # 生成配置到 ~/.config/dbq/（全平台统一）
vim ~/.config/dbq/connections.dev.yaml     # 编辑连接信息（含四种数据库示例）
python scripts/query.py --keychain-set <别名> --env dev  # 存密码（推荐）
```

密码三级查找：Keychain > `.env` 文件 > 环境变量。YAML 中用 `${VAR}` 引用，不存明文。

多环境：`--env dev|test|prod`（默认 dev），每环境独立配置文件 `connections.{env}.yaml`。

## 核心命令

```bash
python scripts/query.py <别名> "SELECT ..."           # 查询（默认 dev）
python scripts/query.py --env prod <别名> "SELECT ..." # 切环境
python scripts/query.py <别名> --show                  # 列出表（含行数+注释）
python scripts/query.py <别名> --desc <表名>           # 表结构（列+索引）
python scripts/query.py <别名> --ddl <表名>            # 建表 DDL
python scripts/query.py <别名> --ping                  # 连接测试
python scripts/query.py --list                         # 列出所有已配置连接
```

常用参数：`--limit N`、`--count`、`--timeout N`、`--format json/csv`、`--dry-run`（预览写操作）、`--multi`（多条 SELECT）。

## 安全机制

- **默认只读**：`readonly: true`，DML 需 `readonly: false`，DDL 需 `allow_ddl: true`
- **四级分级**：READ（始终允许）→ DML（需配置）→ DDL（需配置）→ BLOCKED（始终拒绝）
- **无 WHERE 拦截**：DELETE/UPDATE 不带 WHERE 直接拒绝
- **prod 确认**：生产环境写操作强制确认（`DB_QUERY_ASSUME_YES=1` 跳过）
- **大表保护**：自动 EXPLAIN + 预估行数，无 LIMIT 自动追加 `LIMIT 100`，>50K 行警告

## 依赖

- SQLite：无（标准库）
- MySQL/MariaDB：`pip install pymysql`
- PostgreSQL：`pip install psycopg2-binary`
- YAML 配置：`pip install pyyaml`

## 🔒 安全红线

- **严禁用 Read 工具读取 `~/.config/dbq/connections*.yaml` 或 `.env`** — 密码通过运行时注入
- 所有操作一律通过 `scripts/query.py` 执行
- **严禁删除 `~/.config/dbq/` 下的 .yaml 或 .env 文件**
- 修改用户配置文件前必须先备份
- 清理操作仅限 `/tmp`，绝不触碰配置目录
