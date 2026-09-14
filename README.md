# dbq

dbq 是面向人和 AI Agent 的多数据库 CLI。它用一个连接别名访问 MySQL、MariaDB、PostgreSQL 或 SQLite，支持多项目、多环境、只读查询、结构查看、受控写入和加密配置迁移。

## 设计重点

- 默认只读；DML 和执行 DDL 分别授权。查看 DDL 始终是只读操作。
- 数据库密码只进入 AES-256-GCM 加密的 `vault.dbq`，`conn show` 无法显示密码。
- 主密码只在初始化或迁移时输入。系统凭据库可用时，后续命令自动解锁。
- Windows、macOS、Linux 使用同一份加密格式。复制配置目录后输入原主密码一次即可重新启用自动解锁。
- 查询默认预览 100 行；写入文件表示明确导出，不套用预览行数。
- 默认不增加 SQL 执行超时，适合耗时统计。连接超时默认 10 秒。
- JSON 输出保留列顺序和重复列名；大整数、Decimal、时间和二进制使用稳定编码。

## 安装

需要 Python 3.11 或更高版本。推荐使用 [uv](https://docs.astral.sh/uv/) 创建独立工具环境：

```bash
uv tool install .
dbq --version
dbq doctor
```

也可以使用 pipx：

```bash
pipx install .
```

从 GitHub 安装已发布版本时固定 tag 或 commit：

```bash
uv tool install "git+https://github.com/yinpengfei/skills.git@TAG"
```

安装 Skill 和安装 CLI 是两件事。Skill 告诉 Agent 如何安全调用；CLI 执行确定性的配置、SQL 和加密逻辑。

常见 Agent 可先安装 Skill：

```bash
npx skills add yinpengfei/skills
```

Agent 检测不到 `dbq` 时，可按 Skill 中的安装说明使用固定 release 安装 CLI。不会静默修改系统 Python。

## 首次配置

```bash
dbq init
dbq conn add finance --type mysql --host db.example.com \
  --database finance --user reader
```

初始化会创建 `dev`、`test`、`prod` 三个环境，其中 `prod` 自动标记为生产环境。随机生成主密码的 `dbq init --master-mode generate` 只应由用户在自己的真实终端运行并立即保存；Agent 不应代为运行或读取生成值。

密码通过隐藏式交互输入。自动化使用 stdin，不提供会进入命令历史和进程列表的 `--password` 参数：

```bash
secret-manager-command | dbq conn add finance \
  --type postgresql --host db.example.com --database finance \
  --user reader --password-stdin --non-interactive
```

SQLite：

```bash
dbq conn add local --type sqlite --path ./development.sqlite
```

连接别名是 dbq 配置中的名字，不必等于物理数据库名。`finance` 可以指向不同环境中的不同服务器或数据库。

## 日常查询

最短形式就是“连接别名 + SQL”：

```bash
dbq finance "SELECT * FROM users LIMIT 10"
```

它等价于：

```bash
dbq query finance "SELECT * FROM users LIMIT 10"
dbq q finance "SELECT * FROM users LIMIT 10"
```

当连接别名与 `ddl`、`query` 等保留命令重名时，使用明确的 `query` 形式。

耗时统计默认不会在 30 秒时被 dbq 中断。需要限制或覆盖连接默认值时：

```bash
dbq finance -i monthly.sql -t 20m
dbq finance -i monthly.sql -t 0
dbq conn edit finance --query-timeout 20m
```

优先级是单次命令、连接、无 dbq 额外限制。数据库服务器、代理或管理员设置的限制仍然有效。超时后的 SQL 不会自动重跑；调整 `-t` 后由用户或 Agent 发起新的调用。

长 SQL 和参数绑定：

```bash
dbq finance -i report.sql --params-file report-params.json --json
cat report.sql | dbq finance -i - --json
```

`-i -` 从标准输入读取 SQL。参数文件是 JSON 数组或对象，值交给数据库驱动绑定，不做字符串替换。占位符语法遵循所用驱动。

## 表结构和 DDL

```bash
dbq tables finance --search order
dbq desc finance users orders
dbq ddl finance users orders
dbq inspect finance users orders --ddl --json
```

- MySQL/MariaDB 使用 `SHOW CREATE TABLE`。
- SQLite 返回表、显式索引、触发器和视图定义。
- PostgreSQL 优先调用本机兼容版本的 `pg_dump --schema-only`，密码只通过子进程环境传入；未安装 `pg_dump` 时使用 catalog 重建基础 CREATE TABLE，并明确标记可能缺少的内容。

查看 DDL 不要求连接开放 DDL 写权限。

## 导出和输出

终端默认表格，非交互默认 JSON。AI 应显式使用 `--json`。

```bash
dbq finance "SELECT * FROM users" --json
dbq finance -i report.sql -o report.csv
dbq finance -i report.sql -o report.jsonl
dbq ddl finance 'order_*' -o order-schema.sql
```

显式 SQL 中已有的 LIMIT 保持不变。默认预览限制与数据库扫描量是不同概念；需要控制扫描量时，应在 SQL 中写条件并检查执行计划。

## 凭据、自动解锁和迁移

凭据库使用随机 256 位数据密钥和 AES-256-GCM。主密码通过 Argon2id 派生解锁密钥，后者包装数据密钥。每次加密使用新的 nonce，文件头包含受限且带版本的 KDF 参数。

`keyring` 在 Windows 使用 Windows 凭据后端，在 macOS 使用 Keychain，在有 Secret Service 的 Linux 桌面使用系统凭据库。无桌面的 Linux 通常没有可用后端，可显式提供：

```bash
DBQ_MASTER_PASSWORD_FILE=/run/secrets/dbq-master dbq finance "SELECT 1"
```

也支持 `DBQ_MASTER_PASSWORD`。两者同时设置会失败；显式凭据错误时不会静默回退。

常用管理命令：

```bash
dbq vault status
dbq vault unlock --remember
dbq vault forget
dbq vault password change --remember
```

迁移机器时，停止配置修改后复制整个配置目录，在新机器运行 `dbq vault unlock --remember` 并输入原主密码。默认位置：

- Windows：`%APPDATA%\dbq`
- macOS/Linux：`$XDG_CONFIG_HOME/dbq` 或 `~/.config/dbq`

可用 `dbq config path` 查看实际位置。系统凭据库条目无需复制；主密码是可移植恢复依据。

## 备份和恢复

```bash
dbq backup create -o dbq-settings.dbq-backup
dbq backup verify dbq-settings.dbq-backup
dbq backup restore dbq-settings.dbq-backup --to-dir ./restored
```

普通备份保留原主密码。独立口令备份：

```bash
dbq backup create -o portable.dbq-backup --rekey
```

交互模式会隐藏输入并要求确认独立口令。自动化可将新口令送入 `--backup-password-stdin`；校验和恢复可用 `--password-stdin`。独立口令至少 12 个字符。

备份只包含 dbq 连接配置和加密凭据，不包含业务数据库数据，也不会隐式复制 SQLite 文件。

## 写入

新增连接默认 `readonly: true`、`allow_ddl: false`。确有需要时显式编辑权限，并先预览：

```bash
dbq exec finance --sql-file change.sql --dry-run -e test
dbq exec finance --sql-file change.sql -e prod \
  --confirm-target default/prod/finance
```

UPDATE 和 DELETE 必须有真实顶层 WHERE；恒真条件会被拒绝。生产写入确认只对本次明确目标生效。连接中断导致提交结果未知时，不应自动重试。

## 开发验证

```bash
python -m unittest discover -s tests -v
python -m compileall -q src
python -m pip wheel --no-deps .
```

当前自动测试覆盖加密往返、主密码更换、篡改拒绝、密码不落普通配置、SQL 分句和拦截、SQLite 查询/DDL，以及备份恢复。MySQL、MariaDB、PostgreSQL 和 Windows 系统凭据库仍需要对应平台的集成测试环境。
