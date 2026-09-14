from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from . import __version__
from .backup import create_backup, restore_backup, verify_backup
from .config import ConfigStore, open_store
from .drivers import connect, ddl as fetch_ddl, describe, dialect_for, execute, inspect_details, list_tables, stream_export
from .duration import parse_duration
from .errors import BackupError, ConfigError, DbqError, PolicyError, UsageError
from .io import atomic_write_text, write_private_text_new
from .migrate import migrate as migrate_legacy, preview as preview_legacy
from .result import QueryResult, encode_value, enforce_preview_budget, json_text
from .sql import analyze, apply_preview_limit, count_sql, ensure_write_policy
from .vault import Vault, credential_ref, prompt_new_master, read_master_from_environment

COMMANDS = {
    "init", "config", "env", "conn", "connection", "query", "q", "count", "explain",
    "ex", "exec", "tables", "ls", "desc", "describe", "ddl", "inspect", "info", "vault",
    "backup", "migrate", "doctor", "skills", "help",
}
GLOBAL_VALUE_OPTIONS = {
    "--project": "--project", "-p": "--project", "--env": "--env", "-e": "--env",
    "--config-dir": "--config-dir", "--format": "--format", "-f": "--format",
    "--timeout": "--timeout", "-t": "--timeout",
}
GLOBAL_FLAGS = {"--json", "--non-interactive"}


def _normalize_argv(argv: list[str]) -> list[str]:
    globals_seen: dict[str, str | bool] = {}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        item = argv[i]
        option, equals, inline = item.partition("=")
        if option in GLOBAL_VALUE_OPTIONS:
            canonical = GLOBAL_VALUE_OPTIONS[option]
            if equals:
                value = inline
            else:
                i += 1
                if i >= len(argv):
                    raise UsageError(f"{item} 缺少值")
                value = argv[i]
            old = globals_seen.get(canonical)
            if old is not None and old != value:
                raise UsageError(f"{canonical} 重复且值冲突")
            globals_seen[canonical] = value
        elif item in GLOBAL_FLAGS:
            globals_seen[item] = True
        else:
            rest.append(item)
        i += 1
    prefix: list[str] = []
    for name, value in globals_seen.items():
        prefix.append(name)
        if value is not True:
            prefix.append(str(value))
    if rest and not rest[0].startswith("-") and rest[0] not in COMMANDS:
        rest.insert(0, "query")
    return prefix + rest


def _add_sql_input(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("name")
    parser.add_argument("sql_text", nargs="?")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--sql")
    group.add_argument("--sql-file", "-i", "--file", dest="sql_file")
    parser.add_argument("--params-file")


def _add_connection_options(parser: argparse.ArgumentParser, *, required_type: bool) -> None:
    parser.add_argument("--type", dest="db_type", choices=["mysql", "mariadb", "postgresql", "sqlite"], required=required_type)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--database")
    parser.add_argument("--user")
    parser.add_argument("--path")
    parser.add_argument("--connect-timeout")
    parser.add_argument("--query-timeout")
    parser.add_argument("--tls-mode")
    parser.add_argument("--ca-file")
    access = parser.add_mutually_exclusive_group()
    access.add_argument("--read-write", action="store_true", default=None)
    access.add_argument("--readonly", action="store_true", default=None)
    ddl_access = parser.add_mutually_exclusive_group()
    ddl_access.add_argument("--allow-ddl", action="store_true", default=None)
    ddl_access.add_argument("--deny-ddl", action="store_true", default=None)
    secret = parser.add_mutually_exclusive_group()
    secret.add_argument("--password-stdin", action="store_true")
    secret.add_argument("--no-password", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dbq", description="安全的多数据库命令行工具")
    parser.add_argument("--project", "-p")
    parser.add_argument("--env", "-e")
    parser.add_argument("--config-dir")
    parser.add_argument("--format", "-f", choices=["auto", "table", "json", "csv", "jsonl", "sql"], default="auto")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timeout", "-t")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--version", action="version", version=f"dbq {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--master-mode", choices=["prompt", "generate"], default="prompt")
    init.add_argument("--no-remember", action="store_true")

    config = sub.add_parser("config").add_subparsers(dest="config_command", required=True)
    config.add_parser("path")
    config.add_parser("validate")

    env = sub.add_parser("env").add_subparsers(dest="env_command", required=True)
    env.add_parser("list", aliases=["ls"])
    env_add = env.add_parser("add")
    env_add.add_argument("name")
    env_add.add_argument("--production", action="store_true")

    conn = sub.add_parser("conn", aliases=["connection"]).add_subparsers(dest="conn_command", required=True)
    conn_add = conn.add_parser("add")
    conn_add.add_argument("name")
    _add_connection_options(conn_add, required_type=True)
    conn_edit = conn.add_parser("edit")
    conn_edit.add_argument("name")
    _add_connection_options(conn_edit, required_type=False)
    conn_list = conn.add_parser("list", aliases=["ls"])
    conn_list.add_argument("--all", action="store_true")
    conn_show = conn.add_parser("show")
    conn_show.add_argument("name")
    conn_pass = conn.add_parser("passwd", aliases=["password"])
    conn_pass.add_argument("name")
    conn_pass.add_argument("--password-stdin", action="store_true")
    conn_test = conn.add_parser("test")
    conn_test.add_argument("name")
    conn_rm = conn.add_parser("rm", aliases=["remove"])
    conn_rm.add_argument("name")

    query = sub.add_parser("query", aliases=["q"])
    _add_sql_input(query)
    query.add_argument("--limit", "-n", type=int)
    query.add_argument("--no-limit", action="store_true")
    query.add_argument("--multi", action="store_true")
    query.add_argument("--explain", action="store_true")
    query.add_argument("--output", "-o")

    count = sub.add_parser("count")
    _add_sql_input(count)
    explain = sub.add_parser("explain", aliases=["ex"])
    _add_sql_input(explain)

    write = sub.add_parser("exec")
    _add_sql_input(write)
    write.add_argument("--dry-run", action="store_true")
    write.add_argument("--confirm-target")

    tables = sub.add_parser("tables", aliases=["ls"])
    tables.add_argument("name")
    tables.add_argument("--schema")
    tables.add_argument("--search")
    tables.add_argument("--pattern")

    for command, aliases in (("desc", ["describe"]), ("ddl", []), ("inspect", ["info"])):
        p = sub.add_parser(command, aliases=aliases)
        p.add_argument("name")
        p.add_argument("tables", nargs="*")
        p.add_argument("--all", action="store_true")
        p.add_argument("--schema")
        p.add_argument("--ddl", action="store_true")
        p.add_argument("--output", "-o")

    vault = sub.add_parser("vault").add_subparsers(dest="vault_command", required=True)
    vault.add_parser("status")
    unlock = vault.add_parser("unlock")
    unlock.add_argument("--remember", action="store_true")
    vault.add_parser("forget")
    password = vault.add_parser("password").add_subparsers(dest="password_command", required=True)
    change = password.add_parser("change")
    change.add_argument("--remember", action="store_true")

    backup = sub.add_parser("backup").add_subparsers(dest="backup_command", required=True)
    create = backup.add_parser("create")
    create.add_argument("--output", "-o", required=True)
    create.add_argument("--rekey", action="store_true")
    create.add_argument("--backup-password-stdin", action="store_true")
    verify = backup.add_parser("verify")
    verify.add_argument("file")
    verify.add_argument("--password-stdin", action="store_true")
    restore = backup.add_parser("restore")
    restore.add_argument("file")
    restore.add_argument("--to-dir")
    restore.add_argument("--dry-run", action="store_true")
    restore.add_argument("--password-stdin", action="store_true")

    migrate = sub.add_parser("migrate").add_subparsers(dest="migrate_command", required=True)
    legacy = migrate.add_parser("legacy")
    legacy.add_argument("--source", required=True)
    legacy.add_argument("--dry-run", action="store_true")

    sub.add_parser("doctor")
    sub.add_parser("help")
    skills = sub.add_parser("skills").add_subparsers(dest="skills_command", required=True)
    skills.add_parser("show")
    return parser


def _scope(args, data: dict[str, Any]) -> tuple[str, str]:
    project = args.project or data.get("default_project", "default")
    env = args.env or data.get("default_env", "dev")
    if data.get("environments", {}).get(env, {}).get("production") and args.env is None:
        raise ConfigError(f"生产环境必须显式指定：-e {env}")
    return project, env


def _effective_format(args, *, ddl: bool = False, output: str | None = None) -> str:
    if args.json:
        if args.format not in {"auto", "json"}:
            raise UsageError("--json 与其他 --format 值冲突")
        return "json"
    if args.format != "auto":
        return args.format
    if output:
        suffix = Path(output).suffix.lower()
        mapping = {".csv": "csv", ".json": "json", ".jsonl": "jsonl", ".sql": "sql"}
        if suffix not in mapping:
            raise UsageError("无法从输出文件扩展名判断格式，请显式使用 --format")
        return mapping[suffix]
    if ddl:
        return "sql"
    return "table" if sys.stdout.isatty() else "json"


def _read_sql(args) -> str:
    sources = [args.sql_text is not None, args.sql is not None, args.sql_file is not None]
    if sum(sources) != 1:
        raise UsageError("SQL 位置参数、--sql、--sql-file 必须且只能使用一种")
    if args.sql_file:
        if args.sql_file == "-":
            value = sys.stdin.read(8 * 1024 * 1024 + 1)
            if len(value.encode("utf-8")) > 8 * 1024 * 1024:
                raise UsageError("stdin SQL 文件超过 8 MiB 限制")
            return value
        try:
            path = Path(args.sql_file)
            if path.stat().st_size > 8 * 1024 * 1024:
                raise UsageError("SQL 文件超过 8 MiB 限制")
            return path.read_text(encoding="utf-8")
        except UsageError:
            raise
        except OSError as exc:
            raise UsageError(f"无法读取 SQL 文件：{exc}") from exc
    return args.sql_text if args.sql_text is not None else args.sql


def _read_params(path: str | None) -> object | None:
    if path is None:
        return None
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"参数文件必须是有效 JSON：{exc}") from exc
    if not isinstance(value, (list, dict)):
        raise UsageError("参数文件顶层必须是数组或对象")
    return value


def _password_input(args, *, allow_unchanged: bool = False) -> str | None | object:
    unchanged = object()
    if getattr(args, "no_password", False):
        return None
    if getattr(args, "password_stdin", False):
        value = sys.stdin.readline()
        if not value:
            raise UsageError("stdin 中没有密码")
        return value.rstrip("\r\n")
    if allow_unchanged and args.non_interactive:
        return unchanged
    if args.non_interactive:
        raise UsageError("非交互添加网络连接需要 --password-stdin 或 --no-password")
    return getpass.getpass("数据库密码（输入不回显）：")


def _run_backup_with_unlock(args, operation):
    password = None
    explicit = bool(args.password_stdin) or "DBQ_MASTER_PASSWORD" in os.environ or "DBQ_MASTER_PASSWORD_FILE" in os.environ
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
        if not password:
            raise UsageError("stdin 中没有备份主密码")
    try:
        return operation(password)
    except BackupError:
        if explicit or args.non_interactive:
            raise
        password = getpass.getpass("备份主密码：")
        if not password:
            raise UsageError("备份主密码不能为空")
        return operation(password)


def _validate_connection(info: dict[str, Any]) -> None:
    db_type = info.get("type")
    if db_type == "sqlite":
        if not info.get("path"):
            raise UsageError("SQLite 连接需要 --path")
    elif db_type in {"mysql", "mariadb", "postgresql"}:
        missing = [name for name in ("host", "database", "user") if not info.get(name)]
        if missing:
            raise UsageError("缺少连接字段：" + ", ".join(missing))
        if info.get("tls_mode", "verify-full") not in {"disable", "require", "verify-ca", "verify-full"}:
            raise UsageError("tls-mode 必须是 disable、require、verify-ca 或 verify-full")
        info.setdefault("tls_mode", "verify-full")
    else:
        raise UsageError("无效数据库类型")


def _connection_from_args(args, old: dict[str, Any] | None = None) -> dict[str, Any]:
    info = deepcopy(old or {})
    mapping = {"db_type": "type", "host": "host", "port": "port", "database": "database", "user": "user", "path": "path", "tls_mode": "tls_mode", "ca_file": "ca_file"}
    for source, target in mapping.items():
        value = getattr(args, source, None)
        if value is not None:
            info[target] = value
    for source, target in (("connect_timeout", "connect_timeout"), ("query_timeout", "query_timeout")):
        value = getattr(args, source, None)
        if value is not None:
            info[target] = parse_duration(value)
    if getattr(args, "read_write", None):
        info["readonly"] = False
    elif getattr(args, "readonly", None):
        info["readonly"] = True
    elif "readonly" not in info:
        info["readonly"] = True
    if getattr(args, "allow_ddl", None):
        info["allow_ddl"] = True
    elif getattr(args, "deny_ddl", None):
        info["allow_ddl"] = False
    elif "allow_ddl" not in info:
        info["allow_ddl"] = False
    _validate_connection(info)
    return info


def _unlock_vault(store: ConfigStore, args) :
    vault = Vault(store.directory)
    try:
        return vault.unlock()
    except ConfigError as exc:
        if args.non_interactive or "未解锁" not in str(exc):
            raise
        master = getpass.getpass("dbq 主密码：")
        return vault.unlock(master)


def _unlock_for_connection(store: ConfigStore, info: dict[str, Any], args) -> str | None:
    ref = info.get("credential_ref")
    if not ref:
        return None
    value = _unlock_vault(store, args).get(ref)
    if value is None:
        raise ConfigError("连接凭据缺失；请运行 dbq conn passwd NAME")
    return value


def _target(project: str, env: str, name: str) -> dict[str, str]:
    return {"project": project, "environment": env, "connection": name}


def _render_table(result: QueryResult) -> str:
    names = [str(item["name"]) for item in result.columns]
    if not names:
        return f"影响行数：{result.affected_rows if result.affected_rows is not None else 0}"
    values = [["NULL" if v is None else str(encode_value(v)) for v in row] for row in result.rows]
    widths = [len(n) for n in names]
    for row in values:
        for i, value in enumerate(row):
            widths[i] = min(80, max(widths[i], len(value)))
    lines = [" | ".join(n.ljust(widths[i]) for i, n in enumerate(names)), "-+-".join("-" * w for w in widths)]
    for row in values:
        lines.append(" | ".join(value[: widths[i]].ljust(widths[i]) for i, value in enumerate(row)))
    lines.append(f"({result.row_count} 行{', 已截断' if result.truncated else ''}，{result.elapsed_ms} ms)")
    return "\n".join(lines)


def _emit_result(result: QueryResult, fmt: str, target: dict[str, str], output: str | None = None) -> None:
    if fmt == "table":
        text = _render_table(result)
    elif fmt == "json":
        text = json_text(result.document(target=target))
    elif fmt == "csv":
        import io

        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow([item["name"] for item in result.columns])
        writer.writerows([[encode_value(v) for v in row] for row in result.rows])
        text = stream.getvalue()
    elif fmt == "jsonl":
        text = "\n".join(json.dumps([encode_value(v) for v in row], ensure_ascii=False) for row in result.rows) + "\n"
    else:
        raise UsageError(f"结果不支持格式：{fmt}")
    if output:
        path = Path(output)
        atomic_write_text(path, text)
        manifest = {"schema_version": 1, "status": "ok", "output": str(path.resolve()), "row_count": result.row_count, "elapsed_ms": result.elapsed_ms}
        print(json_text(manifest))
    else:
        print(text, end="" if text.endswith("\n") else "\n")


def _run_query(args, mode: str) -> int:
    store = open_store(args.config_dir)
    data = store.load()
    project, env = _scope(args, data)
    info = store.get_connection(project, env, args.name)
    dialect = dialect_for(info["type"])
    source = _read_sql(args)
    items = analyze(source, dialect, allow_multi=getattr(args, "multi", False))
    params = _read_params(args.params_file)
    if len(items) > 1 and params is not None:
        raise UsageError("--multi 暂不支持共享参数文件")
    if len(items) > 1 and getattr(args, "output", None):
        raise UsageError("--multi 暂不支持单文件导出")
    for item in items:
        ensure_write_policy(item, info, execute=mode == "exec")
    if mode == "exec" and getattr(args, "dry_run", False):
        print(json_text({"schema_version": 1, "status": "dry-run", "target": _target(project, env, args.name), "statements": [item.sql for item in items]}))
        return 0
    if mode == "exec" and store.environment(data, env).get("production"):
        expected = f"{project}/{env}/{args.name}"
        if args.confirm_target != expected:
            raise PolicyError(f"生产写入需要 --confirm-target {expected}")
    password = _unlock_for_connection(store, info, args)
    timeout = parse_duration(args.timeout)
    if timeout is None:
        timeout = info.get("query_timeout")
    run_info = deepcopy(info)
    if timeout is not None:
        run_info["query_timeout"] = timeout
    target = _target(project, env, args.name)
    results: list[QueryResult] = []
    with connect(run_info, password, write=mode == "exec") as connection:
        for item in items:
            sql = item.sql
            injected = False
            preview_limit = None
            if mode == "query":
                if getattr(args, "explain", False):
                    sql = "EXPLAIN " + sql
                else:
                    output = getattr(args, "output", None)
                    preview_limit = None if getattr(args, "no_limit", False) or output else (args.limit or 100)
                    sql, injected = apply_preview_limit(item, preview_limit + 1 if preview_limit else None)
            elif mode == "count":
                sql = count_sql(item)
            elif mode == "explain":
                sql = "EXPLAIN " + item.sql
            if mode == "query" and getattr(args, "output", None):
                fmt = _effective_format(args, output=args.output)
                manifest = stream_export(
                    connection, info["type"], sql, params, timeout_seconds=timeout,
                    output=Path(args.output), fmt=fmt, target=target,
                )
                print(json_text(manifest))
                return 0
            result = execute(connection, info["type"], sql, params, timeout_seconds=timeout, write=mode == "exec")
            if injected and preview_limit is not None and result.row_count > preview_limit:
                result.rows = result.rows[:preview_limit]
                result.row_count = preview_limit
                result.truncated = True
            enforce_preview_budget(result)
            results.append(result)
    fmt = _effective_format(args, output=getattr(args, "output", None))
    if len(results) == 1:
        _emit_result(results[0], fmt, target, getattr(args, "output", None))
    elif fmt == "json":
        print(json_text({"schema_version": 1, "status": "ok", "target": target, "results": [r.document(target=target) for r in results]}))
    else:
        for index, result in enumerate(results, 1):
            print(f"[{index}/{len(results)}]")
            _emit_result(result, fmt, target)
    return 0


def _handle_init(args) -> int:
    store = open_store(args.config_dir)
    vault = Vault(store.directory)
    if store.path.exists() or vault.exists():
        raise ConfigError("dbq 已初始化，不会覆盖现有配置")
    master = prompt_new_master(args.master_mode, non_interactive=args.non_interactive)
    vault.initialize(master, remember=False)
    try:
        store.initialize()
    except Exception:
        vault.path.unlink(missing_ok=True)
        raise
    remembered = False
    if not args.no_remember:
        try:
            vault.remember(master)
            remembered = True
        except ConfigError as exc:
            print(f"警告：{exc}", file=sys.stderr)
    print(json_text({"schema_version": 1, "status": "initialized", "config_dir": str(store.directory), "auto_unlock": remembered}))
    return 0


def _handle_conn(args) -> int:
    store = open_store(args.config_dir)
    data = store.load()
    project, env = _scope(args, data)
    command = args.conn_command
    if command in {"list", "ls"}:
        records = []
        for p, envs in data["connections"].items():
            for e, conns in envs.items():
                if args.all or (p == project and e == env):
                    for name, info in conns.items():
                        records.append({"project": p, "environment": e, "name": name, "type": info["type"], "database": info.get("database") or info.get("path"), "credential": "set" if info.get("credential_ref") else "not-required"})
        print(json_text({"schema_version": 1, "connections": records}))
        return 0
    if command == "show":
        info = store.get_connection(project, env, args.name)
        clean = {k: v for k, v in info.items() if k != "credential_ref"}
        clean["credential"] = "set" if info.get("credential_ref") else "not-required"
        print(json_text({"schema_version": 1, "target": _target(project, env, args.name), "connection": clean}))
        return 0
    if command in {"add", "edit"}:
        old = store.get_connection(project, env, args.name) if command == "edit" else None
        info = _connection_from_args(args, old)
        secret_flag = getattr(args, "password_stdin", False) or getattr(args, "no_password", False)
        ref = credential_ref(project, env, args.name)
        old_ref = old.get("credential_ref") if old else None
        vault_changes: dict[str, str | None] = {}
        if info["type"] == "sqlite":
            info.pop("credential_ref", None)
            if old_ref:
                vault_changes[old_ref] = None
        elif command == "edit" and not secret_flag:
            pass
        else:
            password_marker = _password_input(args)
            if password_marker is None:
                info.pop("credential_ref", None)
                if old_ref:
                    vault_changes[old_ref] = None
            else:
                info["credential_ref"] = ref
                if old_ref and old_ref != ref:
                    vault_changes[old_ref] = None
                vault_changes[ref] = str(password_marker)
        unlocked = None
        old_credentials = None
        if vault_changes:
            unlocked = _unlock_vault(store, args)
            old_credentials = dict(unlocked.credentials)
            for credential, value in vault_changes.items():
                if value is None:
                    unlocked.credentials.pop(credential, None)
                else:
                    unlocked.credentials[credential] = value
            unlocked.save()
        try:
            store.put_connection(project, env, args.name, info, replace=command == "edit")
        except Exception:
            if unlocked is not None and old_credentials is not None:
                unlocked.credentials = old_credentials
                unlocked.save()
            raise
        print(json_text({"schema_version": 1, "status": "saved", "target": _target(project, env, args.name), "credential": "set" if info.get("credential_ref") else "not-required"}))
        return 0
    info = store.get_connection(project, env, args.name)
    ref = info.get("credential_ref")
    if command in {"passwd", "password"}:
        value = _password_input(args)
        new_ref = ref or credential_ref(project, env, args.name)
        unlocked = _unlock_vault(store, args)
        old_credentials = dict(unlocked.credentials)
        unlocked.credentials[new_ref] = str(value)
        unlocked.save()
        try:
            if not ref:
                info["credential_ref"] = new_ref
                store.put_connection(project, env, args.name, info, replace=True)
        except Exception:
            unlocked.credentials = old_credentials
            unlocked.save()
            raise
        print(json_text({"schema_version": 1, "status": "credential-updated", "target": _target(project, env, args.name)}))
        return 0
    if command == "test":
        password = _unlock_for_connection(store, info, args)
        with connect(info, password):
            pass
        print(json_text({"schema_version": 1, "status": "ok", "target": _target(project, env, args.name), "type": info["type"]}))
        return 0
    if command in {"rm", "remove"}:
        unlocked = None
        old_credentials = None
        if ref:
            unlocked = _unlock_vault(store, args)
            old_credentials = dict(unlocked.credentials)
            unlocked.credentials.pop(ref, None)
            unlocked.save()
        try:
            store.remove_connection(project, env, args.name)
        except Exception:
            if unlocked is not None and old_credentials is not None:
                unlocked.credentials = old_credentials
                unlocked.save()
            raise
        print(json_text({"schema_version": 1, "status": "removed", "target": _target(project, env, args.name)}))
        return 0
    raise UsageError("未知 conn 子命令")


def _handle_schema(args) -> int:
    store = open_store(args.config_dir)
    data = store.load()
    project, env = _scope(args, data)
    info = store.get_connection(project, env, args.name)
    password = _unlock_for_connection(store, info, args)
    target = _target(project, env, args.name)
    with connect(info, password) as connection:
        if args.command in {"tables", "ls"}:
            result = list_tables(connection, info["type"], args.schema)
            if args.search:
                needle = args.search.casefold()
                result.rows = [r for r in result.rows if needle in " ".join(str(v) for v in r).casefold()]
            if args.pattern:
                import fnmatch

                result.rows = [r for r in result.rows if fnmatch.fnmatch(str(r[-2] if info["type"] == "postgresql" else r[0]), args.pattern)]
            result.row_count = len(result.rows)
            _emit_result(result, _effective_format(args), target)
            return 0
        import fnmatch

        requested = list(args.tables)
        if args.all:
            requested.append("*")
        if not requested:
            raise UsageError("请提供至少一个表名/通配符，或使用 --all")
        if any("*" in name or "?" in name for name in requested):
            available = list_tables(connection, info["type"], args.schema)
            names = [str(row[1] if info["type"] in {"postgres", "postgresql"} else row[0]) for row in available.rows]
            expanded = []
            for pattern in requested:
                matches = [name for name in names if fnmatch.fnmatch(name, pattern)] if ("*" in pattern or "?" in pattern) else [pattern]
                expanded.extend(matches)
            requested = list(dict.fromkeys(expanded))
            if not requested:
                raise ConfigError("通配符没有匹配任何表")
        documents = []
        for table in requested:
            item = {"table": table}
            if args.command in {"desc", "describe", "inspect", "info"}:
                item["structure"] = describe(connection, info["type"], table, args.schema).document(target=target)
                item["metadata"] = {
                    key: value.document(target=target)
                    for key, value in inspect_details(connection, info["type"], table, args.schema).items()
                }
            if args.command == "ddl" or getattr(args, "ddl", False):
                item["ddl"] = fetch_ddl(
                    connection, info["type"], table, args.schema,
                    connection_info=info, password=password,
                )
            documents.append(item)
    fmt = _effective_format(args, ddl=args.command == "ddl", output=args.output)
    if fmt == "sql":
        text = "\n\n".join(item["ddl"] for item in documents)
        if args.output:
            path = Path(args.output)
            if path.exists():
                raise UsageError(f"输出文件已存在：{path}")
            write_private_text_new(path, text + "\n")
            print(json_text({"schema_version": 1, "status": "ok", "output": str(path.resolve()), "objects": len(documents)}))
        else:
            print(text)
    elif fmt == "json":
        print(json_text({"schema_version": 1, "status": "ok", "target": target, "objects": documents}))
    else:
        for item in documents:
            print(f"{item['table']}:")
            if "structure" in item:
                result_doc = item["structure"]
                result = QueryResult(result_doc["columns"], result_doc["rows"], result_doc["row_count"], result_doc["elapsed_ms"])
                print(_render_table(result))
            for label, metadata in item.get("metadata", {}).items():
                if metadata["row_count"]:
                    print(f"\n{label}:")
                    result = QueryResult(metadata["columns"], metadata["rows"], metadata["row_count"], metadata["elapsed_ms"])
                    print(_render_table(result))
            if "ddl" in item:
                print(item["ddl"])
    return 0


def _handle_vault(args) -> int:
    store = open_store(args.config_dir)
    vault = Vault(store.directory)
    if args.vault_command == "status":
        auto = False
        unlocked = False
        if vault.exists():
            try:
                vault.unlock()
                auto = unlocked = True
            except ConfigError:
                pass
        print(json_text({"schema_version": 1, "exists": vault.exists(), "unlocked": unlocked, "auto_unlock": auto}))
    elif args.vault_command == "forget":
        vault.forget()
        print(json_text({"schema_version": 1, "status": "forgotten"}))
    elif args.vault_command == "unlock":
        master = read_master_from_environment() if args.non_interactive else getpass.getpass("主密码：")
        if master is None:
            raise UsageError("缺少主密码")
        vault.unlock(master)
        if args.remember:
            vault.remember(master)
        print(json_text({"schema_version": 1, "status": "unlocked", "remembered": args.remember}))
    elif args.vault_command == "password" and args.password_command == "change":
        if args.non_interactive:
            raise UsageError("主密码修改目前需要交互终端")
        old = getpass.getpass("当前主密码：")
        new = getpass.getpass("新主密码：")
        confirm = getpass.getpass("再次输入新主密码：")
        if new != confirm or len(new) < 12:
            raise UsageError("新主密码不一致或少于 12 个字符")
        vault.change_password(old, new, remember=args.remember)
        print(json_text({"schema_version": 1, "status": "password-changed", "remembered": args.remember}))
    return 0


def dispatch(args) -> int:
    if args.command == "init":
        return _handle_init(args)
    if args.command == "config":
        store = open_store(args.config_dir)
        if args.config_command == "path":
            print(store.directory)
        else:
            store.load()
            print(json_text({"schema_version": 1, "status": "valid", "path": str(store.path)}))
        return 0
    if args.command == "env":
        store = open_store(args.config_dir)
        data = store.load()
        if args.env_command in {"list", "ls"}:
            print(json_text({"schema_version": 1, "environments": data["environments"]}))
        else:
            if args.name in data["environments"]:
                raise ConfigError(f"环境已存在：{args.name}")
            data["environments"][args.name] = {"production": args.production}
            store.save(data)
            print(json_text({"schema_version": 1, "status": "created", "environment": args.name, "production": args.production}))
        return 0
    if args.command in {"conn", "connection"}:
        return _handle_conn(args)
    if args.command in {"query", "q"}:
        return _run_query(args, "query")
    if args.command == "count":
        return _run_query(args, "count")
    if args.command in {"explain", "ex"}:
        return _run_query(args, "explain")
    if args.command == "exec":
        return _run_query(args, "exec")
    if args.command in {"tables", "ls", "desc", "describe", "ddl", "inspect", "info"}:
        return _handle_schema(args)
    if args.command == "vault":
        return _handle_vault(args)
    if args.command == "backup":
        store = open_store(args.config_dir)
        if args.backup_command == "create":
            if args.backup_password_stdin and not args.rekey:
                raise UsageError("--backup-password-stdin 只能与 --rekey 一起使用")
            unlocked = None
            password = None
            if args.rekey:
                unlocked = _unlock_vault(store, args)
                if args.backup_password_stdin:
                    password = sys.stdin.readline().rstrip("\r\n")
                    if not password:
                        raise UsageError("stdin 中没有独立备份口令")
                elif args.non_interactive:
                    raise UsageError("--rekey 非交互模式需要 --backup-password-stdin")
                else:
                    password = getpass.getpass("独立备份口令：")
                    confirm = getpass.getpass("再次输入独立备份口令：")
                    if password != confirm:
                        raise UsageError("两次输入的独立备份口令不一致")
                if len(password) < 12:
                    raise UsageError("独立备份口令至少需要 12 个字符")
            create_backup(store.directory, Path(args.output), unlocked=unlocked, backup_password=password)
            print(json_text({"schema_version": 1, "status": "created", "backup": str(Path(args.output).resolve()), "rekeyed": args.rekey}))
        elif args.backup_command == "verify":
            manifest = _run_backup_with_unlock(
                args, lambda password: verify_backup(Path(args.file), master_password=password)
            )
            print(json_text({"schema_version": 1, "status": "valid", "manifest": manifest}))
        else:
            destination = Path(args.to_dir).expanduser().resolve() if args.to_dir else store.directory
            manifest = _run_backup_with_unlock(
                args,
                lambda password: restore_backup(
                    Path(args.file), destination, dry_run=args.dry_run, master_password=password
                ),
            )
            print(json_text({"schema_version": 1, "status": "dry-run" if args.dry_run else "restored", "destination": str(destination), "manifest": manifest}))
        return 0
    if args.command == "doctor":
        store = open_store(args.config_dir)
        dependencies = {}
        for name in ("cryptography", "keyring", "sqlglot", "pymysql", "psycopg"):
            try:
                __import__(name)
                dependencies[name] = "ok"
            except Exception:
                dependencies[name] = "missing"
        print(json_text({"schema_version": 1, "python": sys.version.split()[0], "platform": sys.platform, "config_dir": str(store.directory), "config": "present" if store.path.exists() else "missing", "vault": "present" if Vault(store.directory).exists() else "missing", "dependencies": dependencies}))
        return 0
    if args.command == "migrate":
        source = Path(args.source).expanduser().resolve()
        if args.dry_run:
            records = preview_legacy(source)
            status = "dry-run"
        else:
            store = open_store(args.config_dir)
            records = migrate_legacy(source, store, _unlock_vault(store, args))
            status = "migrated"
        print(json_text({"schema_version": 1, "status": status, "source": str(source), "connections": records, "source_preserved": True}))
        return 0
    if args.command == "skills":
        print("使用 dbq 完成数据库任务：先确认连接与环境；优先 inspect/ddl 获取结构；query 默认只读并限制预览；写入只走 exec，生产目标需要精确确认；绝不读取或回显配置目录中的凭据。")
        return 0
    if args.command == "help":
        build_parser().print_help()
        return 0
    raise UsageError("未知命令")


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        normalized = _normalize_argv(raw)
        parser = build_parser()
        args = parser.parse_args(normalized)
        return dispatch(args)
    except DbqError as exc:
        wants_json = "--json" in raw or any(item in {"--format=json", "-f=json"} for item in raw)
        payload = {"schema_version": 1, "status": "error", "error": {"type": type(exc).__name__, "message": str(exc), "exit_code": exc.exit_code}}
        print(json_text(payload) if wants_json else f"dbq: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("dbq: 已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
