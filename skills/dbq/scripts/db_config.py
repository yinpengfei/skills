#!/usr/bin/env python3
"""dbq 配置管理 —— 路径常量 / 密码解析 / 环境配置加载 / 连接获取。"""

from __future__ import annotations

import json as _json_mod
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

# ── 跨平台配置目录 ──────────────────────────────────────────
#
# 全平台统一: ~/.config/dbq/
#   macOS:   /Users/<user>/.config/dbq/
#   Linux:   /home/<user>/.config/dbq/
#   Windows: C:\Users\<user>\.config\dbq\
#

def _get_config_dir() -> Path:
    """返回 dbq 配置目录，全平台统一为 ~/.config/dbq/"""
    return Path.home() / ".config" / "dbq"


# ── 路径常量 ─────────────────────────────────────────
SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = _get_config_dir()
ASSETS_DIR = CONFIG_DIR  # 兼容别名，部分模块仍引用此名称
ENV_FILE = CONFIG_DIR / ".env"
LOG_DIR = CONFIG_DIR / "logs"
SQLITE_TEST_DB = CONFIG_DIR / "sqlite_test.db"

DEFAULT_ENV = os.environ.get("DB_QUERY_DEFAULT_ENV", "dev")

# ── 配置模板（内嵌，不依赖 assets/ 目录文件）─────────────────
# --init-config 自动生成，已存在则跳过。
# SQLite 测试库路径: {CONFIG_DIR}/sqlite_test.db

_config_dir_display = str(CONFIG_DIR)

_CONFIG_TEMPLATES = {
    "connections.dev.yaml": rf"""# 开发环境数据库连接配置
# ⚠️  三种密码管理方式（值字段支持 ${{VAR}} 占位符）:
#   1. 不写 password 字段 → 通过 Keychain/.env/环境变量 约定查找 DB_PWD_{{ENV}}_{{ALIAS}}
#   2. password: ${{MY_SHARED_PASS}} → 从 .env 或环境变量中解析（多库共用推荐）
#   3. password: ${{ENV_VAR}} → 直接引用父进程环境变量
#
# 已包含开箱即用的测试连接:
#   sqlite_test — SQLite 测试库（无需安装任何依赖）
#   mysql        — 本机 MySQL（无密码，需先安装 MySQL 并启动服务）
#
# 查询: python scripts/query.py sqlite_test "SELECT 1"
# 查询: python scripts/query.py mysql "SELECT 1"  （需先配置密码或改为无密码）

connections:
  # ─── SQLite 测试库（开箱即用，无需额外依赖）─────────
  sqlite_test:
    type: sqlite
    path: "{_config_dir_display}/sqlite_test.db"
    readonly: false

  # ─── 本机 MySQL（无密码，需先 brew install mysql && brew services start mysql）──
  mysql:
    type: mysql
    host: 127.0.0.1
    port: 3306
    user: root
    database: mysql
    readonly: true
  # 无密码时取消下面一行的注释（MySQL 8+ 默认有密码，需先设置）:
  # password: ""

  # ─── 示例：多库共享密码（推荐） ──
  # recharge_db:
  #   type: mysql
  #   host: 10.18.122.60
  #   port: 3306
  #   user: readonly_dev
  #   password: ${{PWD_DEV}}
  #   database: recharge
  #   charset: utf8mb4
  #   connect_timeout: 10
  #   readonly: true
""",

    "connections.test.yaml": r"""# 测试环境数据库连接配置
# 用法同 connections.dev.yaml

connections:
  # recharge_db:
  #   type: mysql
  #   host: 10.18.122.61
  #   port: 3306
  #   user: readonly
  #   password: ${{PWD_TEST}}
  #   database: recharge
  #
  # pay_db:
  #   type: mysql
  #   host: 10.18.122.61
  #   port: 3306
  #   user: readonly
  #   password: ${{PWD_TEST}}
  #   database: pay
""",

    "connections.prod.yaml": r"""# 生产环境数据库连接配置（独立文件，建议 chmod 600）
# 用法同 connections.dev.yaml

connections:
  # recharge_db:
  #   type: mysql
  #   host: 10.19.xx.xx
  #   port: 3306
  #   user: readonly
  #   password: ${{PWD_PROD}}
  #   database: recharge
  #
  # pay_db:
  #   type: mysql
  #   host: 10.19.xx.xx
  #   port: 3306
  #   user: readonly
  #   password: ${{PWD_PROD}}
  #   database: pay
""",

    ".env": r"""# 数据库密码配置文件
# ⚠️  包含敏感信息，不要提交到 Git！
# 编辑填入实际密码后: chmod 600 .env
#
# 两种使用方式:
#
# --- 方式 1: 按约定命名（脚本自动查找） ---
# 规则: DB_PWD_{{ENV}}_{{ALIAS大写 短横换下划线}}
# DB_PWD_DEV_RECHARGE_DB=dev_password_here
# DB_PWD_TEST_RECHARGE_DB=test_password_here
#
# --- 方式 2: 自定义变量名 + YAML ${{VAR}} 引用（多库共享推荐） ---
# PWD_DEV=shared_dev_password
# PWD_TEST=shared_test_password
# PWD_PROD=shared_prod_password
# 在 connections.{env}.yaml 中用 password: ${{PWD_PROD}} 引用
""",
}


def _create_sqlite_test_db(path: Path) -> None:
    """创建 SQLite 测试库并写入示例数据。已存在则跳过。"""
    if path.exists():
        return
    conn = sqlite3.connect(str(path))
    cur = conn.cursor()
    # ── 用户表 ──────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT    NOT NULL,
            email     TEXT    UNIQUE,
            status    INTEGER DEFAULT 1,
            created_at TEXT    DEFAULT (datetime('now'))
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_users_status ON users(status)")
    # ── 订单表 ──────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER NOT NULL,
            amount    REAL    DEFAULT 0.0,
            status    TEXT    DEFAULT 'pending',
            created_at TEXT    DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
    # ── 商品表 ──────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            price       REAL    NOT NULL,
            stock       INTEGER DEFAULT 0,
            description TEXT
        )
    """)
    # ── 示例数据 ────────────────────────
    cur.executemany(
        "INSERT OR IGNORE INTO users (id, name, email, status) VALUES (?, ?, ?, ?)",
        [
            (1, "张三", "zhangsan@example.com", 1),
            (2, "李四", "lisi@example.com", 1),
            (3, "王五", "wangwu@example.com", 0),
        ],
    )
    cur.executemany(
        "INSERT OR IGNORE INTO orders (id, user_id, amount, status) VALUES (?, ?, ?, ?)",
        [
            (1, 1, 99.9, "paid"),
            (2, 1, 25.0, "pending"),
            (3, 2, 199.0, "paid"),
        ],
    )
    cur.executemany(
        "INSERT OR IGNORE INTO products (id, name, price, stock, description) VALUES (?, ?, ?, ?, ?)",
        [
            (1, "蓝牙耳机", 199.0, 50, "降噪无线蓝牙耳机"),
            (2, "数据线", 29.9, 500, "USB-C 快充数据线"),
            (3, "充电宝", 149.0, 100, "20000mAh 移动电源"),
        ],
    )
    conn.commit()
    conn.close()


def _handle_init_config():
    """生成配置模板 + SQLite 测试库到配置目录，已存在则跳过。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    created, skipped = [], []

    for filename, content in _CONFIG_TEMPLATES.items():
        target = CONFIG_DIR / filename
        if target.exists():
            skipped.append(filename)
        else:
            target.write_text(content, encoding="utf-8")
            created.append(filename)
            if "prod" in filename:
                try:
                    target.chmod(0o600)
                except OSError:
                    pass

    # 创建 SQLite 测试库
    sqlite_created = False
    if not SQLITE_TEST_DB.exists():
        _create_sqlite_test_db(SQLITE_TEST_DB)
        sqlite_created = True

    if created:
        print(f"✅ 已创建 {len(created)} 个配置文件（{CONFIG_DIR}/）:")
        for f in created:
            print(f"   {CONFIG_DIR.name}/{f}")
    if skipped:
        print(f"⏭️  跳过 {len(skipped)} 个（已存在）:")
        for f in skipped:
            print(f"   {CONFIG_DIR.name}/{f}")
    if sqlite_created:
        print(f"✅ 已创建 SQLite 测试库: {SQLITE_TEST_DB}")
    elif SQLITE_TEST_DB.exists():
        print(f"⏭️  SQLite 测试库已存在: {SQLITE_TEST_DB}")

    print(f"\n配置目录: {CONFIG_DIR}")
    print("\n下一步:")
    print(f"  1. 编辑 {CONFIG_DIR}/connections.dev.yaml 填入你的数据库连接信息")
    print(f"  2. （可选）编辑 {CONFIG_DIR}/.env 填入密码")
    print("  3. 测试: python scripts/query.py sqlite_test \"SELECT * FROM users\"")
    print("密码管理: Keychain 自动查找 > .env 文件 > 环境变量")


def _config_file_for(env: str) -> Path:
    """env → connections.{env}.yaml"""
    return CONFIG_DIR / f"connections.{env}.yaml"


# ── YAML 依赖检查 ───────────────────────────────────────────

try:
    import yaml
except ImportError:
    yaml = None


# ── 密码解析 ────────────────────────────────────────────────
# 优先级: macOS Keychain > .env 文件 > 父进程环境变量
# Keychain 条目: service=dbq/{env}/{alias}
# .env 变量名:   DB_PWD_{{ENV}}_{{ALIAS}}  (全大写，短横换下划线)

def _keychain_service(env: str, alias: str) -> str:
    return f"dbq/{env}/{alias}"


def _dotenv_var(env: str, alias: str) -> str:
    raw = f"{env}_{alias}".upper().replace("-", "_")
    return f"DB_PWD_{raw}"


def _load_dotenv(path: Path) -> dict:
    """解析 .env 文件为 dict"""
    env = {}
    if not path.exists():
        return env
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            env[k] = v
    return env


def _resolve_password(env: str, alias: str) -> str:
    """按优先级获取密码"""
    service = _keychain_service(env, alias)
    env_var = _dotenv_var(env, alias)

    # 1 — macOS Keychain
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                [
                    "security", "find-generic-password",
                    "-a", "dbq",
                    "-s", service,
                    "-w",
                ],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # 2 — .env 文件
    dotenv = _load_dotenv(ENV_FILE)
    if env_var in dotenv:
        return dotenv[env_var]

    # 3 — 父进程环境变量
    if env_var in os.environ:
        return os.environ[env_var]

    raise RuntimeError(
        f"无法获取 [{alias}] ({env} 环境) 的密码。请通过以下任一方式配置:\n"
        f"  1. Keychain: security add-generic-password -a dbq -s {service} -w '密码'\n"
        f"  2. {CONFIG_DIR.name}/.env: {env_var}=密码\n"
        f"  3. 环境变量: export {env_var}=密码"
    )


# ── 环境配置加载 ────────────────────────────────────────────

def _resolve_placeholders(data):
    """递归替换数据中所有字符串值里的 ${{VAR}} 占位符。

    查找顺序: .env 文件 → 父进程环境变量
    未找到的占位符保持原样并打印警告。
    """
    dotenv = _load_dotenv(ENV_FILE)

    def _replace(val):
        if not isinstance(val, str):
            return val

        def _lookup(match):
            var = match.group(1)
            if var in dotenv:
                return dotenv[var]
            if var in os.environ:
                return os.environ[var]
            print(
                f"[WARN] 占位符 ${{{var}}} 在 .env 和环境变量中均未找到，保持原样",
                file=sys.stderr,
            )
            return match.group(0)
        return re.sub(r"\$\{(\w+)\}", _lookup, val)

    return _walk_replace(data, _replace)


def _walk_replace(data, fn):
    """递归遍历 dict/list，对每个字符串值应用 fn"""
    if isinstance(data, dict):
        return {k: _walk_replace(v, fn) for k, v in data.items()}
    elif isinstance(data, list):
        return [_walk_replace(v, fn) for v in data]
    else:
        return fn(data)


def _load_yaml_file(path: Path) -> dict:
    if yaml is None:
        raise ImportError(
            "需要 PyYAML 来读取 YAML 配置。请安装: pip install pyyaml"
        )
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_json_file(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return _json_mod.load(f) or {}


def _load_any_config(path: Path) -> dict:
    """加载 YAML 或 JSON 配置文件"""
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        return _load_yaml_file(path)
    elif suffix == ".json":
        return _load_json_file(path)
    raise ValueError(f"不支持的配置格式: {suffix}")


def load_env_config(env: str, config_override: str | None = None) -> dict:
    """加载环境的连接配置，返回 connections 字典。

    1. --config 指定 → 加载该文件
    2. --env dev|test|prod → connections.{env}.yaml

    加载后会对所有值进行 ${{VAR}} 占位符替换，优先查 .env 文件，其次查环境变量。
    """
    if config_override:
        path = Path(config_override)
        if not path.is_absolute():
            path = CONFIG_DIR / path
    else:
        path = _config_file_for(env)

    if not path.exists():
        if config_override:
            raise FileNotFoundError(f"配置文件不存在: {path}")
        raise FileNotFoundError(
            f"环境 [{env}] 配置文件不存在: {path.name}\n"
            f"请先运行: python scripts/query.py --init-config"
        )
    config = _load_any_config(path)
    connections = config.get("connections", {})
    return _resolve_placeholders(connections)


# ── 连接获取 ────────────────────────────────────────────────

def get_connection(db_alias: str, env: str, config_override: str | None = None) -> dict:
    """获取单个数据库连接信息（含密码注入）"""
    connections = load_env_config(env, config_override)
    if db_alias not in connections:
        available = list(connections.keys())
        raise ValueError(
            f"未找到数据库别名 [{db_alias}] ({env} 环境)\n"
            f"可用别名: {', '.join(available) if available else '(无)'}"
        )
    conn = dict(connections[db_alias])  # 浅拷贝，避免污染缓存
    db_type = conn.get("type", "").lower()
    if db_type in ("sqlite", "sqlite3"):
        # SQLite 只需要 type + path，默认 :memory:
        conn.setdefault("path", ":memory:")
    else:
        required = ["type", "host", "port", "user", "database"]
        missing = [k for k in required if k not in conn]
        if missing:
            raise ValueError(
                f"数据库 {db_alias} 配置不完整，缺少字段: {', '.join(missing)}"
            )
        # 密码: YAML 中已解析的 ${{VAR}} 优先，否则走约定查找
        if not conn.get("password"):
            conn["password"] = _resolve_password(env, db_alias)
    return conn
