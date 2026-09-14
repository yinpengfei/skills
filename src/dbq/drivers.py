from __future__ import annotations

import os
import shutil
import sqlite3
import ssl as ssl_module
import subprocess
import tempfile
import csv
import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import ConfigError, ConnectionError, DbqError, QueryError, TimeoutError, UnknownWriteResultError
from .result import QueryResult, encode_value


def dialect_for(db_type: str) -> str:
    return {"postgres": "postgres", "postgresql": "postgres", "mariadb": "mysql"}.get(
        db_type, db_type
    )


def quote_identifier(value: str, db_type: str) -> str:
    if "\x00" in value:
        raise ConfigError("标识符包含无效字符")
    if db_type in {"mysql", "mariadb"}:
        return "`" + value.replace("`", "``") + "`"
    return '"' + value.replace('"', '""') + '"'


def _sqlite_path(value: str, *, readonly: bool) -> tuple[str, bool]:
    if value == ":memory:":
        return value, False
    path = Path(value).expanduser().resolve()
    if not path.exists():
        raise ConnectionError(f"SQLite 文件不存在：{path}")
    if readonly:
        return path.as_uri() + "?mode=ro", True
    return str(path), False


@contextmanager
def connect(info: dict[str, Any], password: str | None, *, write: bool = False) -> Iterator[Any]:
    db_type = info["type"]
    timeout = float(info.get("connect_timeout", 10))
    connection = None
    try:
        if db_type == "sqlite":
            name, uri = _sqlite_path(str(info["path"]), readonly=bool(info.get("readonly", True)))
            connection = sqlite3.connect(name, timeout=timeout, uri=uri)
            connection.execute("PRAGMA query_only = OFF" if write else "PRAGMA query_only = ON")
        elif db_type in {"mysql", "mariadb"}:
            try:
                import pymysql
            except ImportError as exc:
                raise ConfigError("缺少 MySQL/MariaDB 驱动，请重新安装完整 dbq") from exc
            kwargs = {
                "host": info["host"],
                "port": int(info.get("port", 3306)),
                "user": info["user"],
                "database": info["database"],
                "charset": info.get("charset", "utf8mb4"),
                "connect_timeout": timeout,
                "autocommit": not write,
            }
            if password is not None:
                kwargs["password"] = password
            if query_timeout := info.get("query_timeout"):
                kwargs["read_timeout"] = float(query_timeout)
                kwargs["write_timeout"] = float(query_timeout)
            tls_mode = info.get("tls_mode", "verify-full")
            if tls_mode != "disable":
                context = ssl_module.create_default_context(cafile=info.get("ca_file"))
                if tls_mode == "require":
                    context.check_hostname = False
                    context.verify_mode = ssl_module.CERT_NONE
                elif tls_mode == "verify-ca":
                    context.check_hostname = False
                kwargs["ssl"] = context
            connection = pymysql.connect(**kwargs)
            if not write:
                with connection.cursor() as cursor:
                    cursor.execute("SET SESSION TRANSACTION READ ONLY")
        elif db_type in {"postgres", "postgresql"}:
            try:
                import psycopg
            except ImportError as exc:
                raise ConfigError("缺少 PostgreSQL 驱动，请重新安装完整 dbq") from exc
            kwargs = {
                "host": info["host"],
                "port": int(info.get("port", 5432)),
                "user": info["user"],
                "dbname": info["database"],
                "connect_timeout": int(timeout),
                "autocommit": not write,
            }
            if password is not None:
                kwargs["password"] = password
            kwargs["sslmode"] = info.get("tls_mode", "verify-full")
            if info.get("ca_file"):
                kwargs["sslrootcert"] = info["ca_file"]
            connection = psycopg.connect(**kwargs)
            if not write:
                connection.read_only = True
        else:
            raise ConfigError(f"不支持的数据库类型：{db_type}")
        yield connection
    except DbqError:
        raise
    except Exception as exc:
        raise ConnectionError(f"数据库连接失败（{type(exc).__name__}）") from exc
    finally:
        if connection is not None:
            connection.close()


def execute(
    connection: Any,
    db_type: str,
    sql: str,
    params: object | None,
    *,
    timeout_seconds: float | None,
    write: bool,
) -> QueryResult:
    started = time.monotonic()
    timed_out = False
    if timeout_seconds and db_type == "sqlite":
        deadline = started + timeout_seconds

        def progress() -> int:
            nonlocal timed_out
            timed_out = time.monotonic() >= deadline
            return int(timed_out)

        connection.set_progress_handler(progress, 1000)
    cursor = connection.cursor()
    try:
        if timeout_seconds and db_type in {"postgres", "postgresql"}:
            cursor.execute(f"SET statement_timeout = {int(timeout_seconds * 1000)}")
        cursor.execute(sql, params) if params is not None else cursor.execute(sql)
        columns = []
        rows: list[list[Any]] = []
        if cursor.description:
            columns = [
                {"name": item.name if hasattr(item, "name") else item[0], "type": str(item.type_code if hasattr(item, "type_code") else item[1])}
                for item in cursor.description
            ]
            rows = [list(row) for row in cursor.fetchall()]
        affected = cursor.rowcount if cursor.rowcount >= 0 else None
        if write:
            try:
                connection.commit()
            except Exception as exc:
                raise UnknownWriteResultError(
                    "提交期间连接失败，结果未知；请核对数据库状态，禁止自动重试"
                ) from exc
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            affected_rows=affected if write else None,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )
    except sqlite3.OperationalError as exc:
        if timed_out:
            if write:
                connection.rollback()
            raise TimeoutError("SQL 执行超时并已取消") from exc
        if write:
            connection.rollback()
        raise QueryError(f"SQL 执行失败：{exc}") from exc
    except UnknownWriteResultError:
        raise
    except Exception as exc:
        if write:
            try:
                connection.rollback()
            except Exception:
                pass
        name = type(exc).__name__
        if "timeout" in name.lower() or "cancel" in name.lower():
            raise TimeoutError("SQL 执行超时或已取消") from exc
        raise QueryError(f"SQL 执行失败（{name}）") from exc
    finally:
        if db_type == "sqlite":
            connection.set_progress_handler(None, 0)
        cursor.close()


def stream_export(
    connection: Any,
    db_type: str,
    sql: str,
    params: object | None,
    *,
    timeout_seconds: float | None,
    output: Path,
    fmt: str,
    target: dict[str, str],
) -> dict[str, Any]:
    if fmt not in {"csv", "json", "jsonl"}:
        raise ConfigError("查询导出格式必须是 csv、json 或 jsonl")
    if output.exists():
        raise ConfigError(f"输出文件已存在：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    if os.name != "nt":
        os.fchmod(fd, 0o600)
    os.close(fd)
    temporary = Path(temporary_name)
    started = time.monotonic()
    timed_out = False
    if timeout_seconds and db_type == "sqlite":
        deadline = started + timeout_seconds

        def progress() -> int:
            nonlocal timed_out
            timed_out = time.monotonic() >= deadline
            return int(timed_out)

        connection.set_progress_handler(progress, 1000)
    if db_type in {"mysql", "mariadb"}:
        import pymysql

        cursor = connection.cursor(pymysql.cursors.SSCursor)
    else:
        cursor = connection.cursor()
    count = 0
    try:
        if timeout_seconds and db_type in {"postgres", "postgresql"}:
            cursor.execute(f"SET statement_timeout = {int(timeout_seconds * 1000)}")
        cursor.execute(sql, params) if params is not None else cursor.execute(sql)
        columns = [
            {"name": item.name if hasattr(item, "name") else item[0], "type": str(item.type_code if hasattr(item, "type_code") else item[1])}
            for item in (cursor.description or [])
        ]
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            if fmt == "csv":
                writer = csv.writer(stream)
                writer.writerow([item["name"] for item in columns])
            elif fmt == "json":
                stream.write(json.dumps({"schema_version": 1, "status": "ok", "target": target, "columns": columns}, ensure_ascii=False)[:-1])
                stream.write(', "rows": [')
            first = True
            while True:
                batch = cursor.fetchmany(1000)
                if not batch:
                    break
                for row in batch:
                    encoded = [encode_value(value) for value in row]
                    if fmt == "csv":
                        writer.writerow([json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in encoded])
                    elif fmt == "jsonl":
                        stream.write(json.dumps(encoded, ensure_ascii=False) + "\n")
                    else:
                        if not first:
                            stream.write(",")
                        stream.write(json.dumps(encoded, ensure_ascii=False))
                        first = False
                    count += 1
            elapsed = round((time.monotonic() - started) * 1000)
            if fmt == "json":
                stream.write(f'], "row_count": {count}, "truncated": false, "elapsed_ms": {elapsed}}}')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        return {"schema_version": 1, "status": "ok", "output": str(output.resolve()), "row_count": count, "elapsed_ms": elapsed}
    except sqlite3.OperationalError as exc:
        temporary.unlink(missing_ok=True)
        if timed_out:
            raise TimeoutError("SQL 导出超时并已取消") from exc
        raise QueryError(f"SQL 导出失败：{exc}") from exc
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        if isinstance(exc, (ConfigError, QueryError, TimeoutError)):
            raise
        raise QueryError(f"SQL 导出失败（{type(exc).__name__}）") from exc
    finally:
        if db_type == "sqlite":
            connection.set_progress_handler(None, 0)
        cursor.close()


def list_tables(connection: Any, db_type: str, schema: str | None = None) -> QueryResult:
    if db_type == "sqlite":
        sql = "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
        return execute(connection, db_type, sql, None, timeout_seconds=None, write=False)
    if db_type in {"mysql", "mariadb"}:
        sql = "SELECT TABLE_NAME, TABLE_TYPE, TABLE_COMMENT FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME"
        return execute(connection, db_type, sql, None, timeout_seconds=None, write=False)
    sql = "SELECT table_schema, table_name, table_type FROM information_schema.tables WHERE table_schema = COALESCE(%s, current_schema()) ORDER BY table_name"
    return execute(connection, db_type, sql, (schema,), timeout_seconds=None, write=False)


def describe(connection: Any, db_type: str, table: str, schema: str | None = None) -> QueryResult:
    if db_type == "sqlite":
        sql = f"PRAGMA table_xinfo({quote_identifier(table, db_type)})"
        return execute(connection, db_type, sql, None, timeout_seconds=None, write=False)
    if db_type in {"mysql", "mariadb"}:
        sql = f"SHOW FULL COLUMNS FROM {quote_identifier(table, db_type)}"
        return execute(connection, db_type, sql, None, timeout_seconds=None, write=False)
    sql = """SELECT c.ordinal_position, c.column_name, c.data_type, c.udt_name,
                    c.is_nullable, c.column_default, c.is_identity, c.identity_generation
             FROM information_schema.columns c
             WHERE c.table_schema = COALESCE(%s, current_schema()) AND c.table_name = %s
             ORDER BY c.ordinal_position"""
    return execute(connection, db_type, sql, (schema, table), timeout_seconds=None, write=False)


def inspect_details(connection: Any, db_type: str, table: str, schema: str | None = None) -> dict[str, QueryResult]:
    if db_type == "sqlite":
        qtable = quote_identifier(table, db_type)
        return {
            "indexes": execute(connection, db_type, f"PRAGMA index_list({qtable})", None, timeout_seconds=None, write=False),
            "foreign_keys": execute(connection, db_type, f"PRAGMA foreign_key_list({qtable})", None, timeout_seconds=None, write=False),
        }
    if db_type in {"mysql", "mariadb"}:
        indexes = execute(
            connection, db_type, f"SHOW INDEX FROM {quote_identifier(table, db_type)}", None,
            timeout_seconds=None, write=False,
        )
        constraints = execute(
            connection,
            db_type,
            """SELECT tc.CONSTRAINT_NAME, tc.CONSTRAINT_TYPE, kcu.COLUMN_NAME,
                      kcu.REFERENCED_TABLE_NAME, kcu.REFERENCED_COLUMN_NAME
               FROM information_schema.TABLE_CONSTRAINTS tc
               LEFT JOIN information_schema.KEY_COLUMN_USAGE kcu
                 ON tc.CONSTRAINT_SCHEMA=kcu.CONSTRAINT_SCHEMA
                AND tc.TABLE_NAME=kcu.TABLE_NAME
                AND tc.CONSTRAINT_NAME=kcu.CONSTRAINT_NAME
               WHERE tc.CONSTRAINT_SCHEMA=DATABASE() AND tc.TABLE_NAME=%s
               ORDER BY tc.CONSTRAINT_NAME, kcu.ORDINAL_POSITION""",
            (table,), timeout_seconds=None, write=False,
        )
        return {"indexes": indexes, "constraints": constraints}
    indexes = execute(
        connection,
        db_type,
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname=COALESCE(%s,current_schema()) AND tablename=%s ORDER BY indexname",
        (schema, table), timeout_seconds=None, write=False,
    )
    constraints = execute(
        connection,
        db_type,
        """SELECT c.conname, c.contype, pg_get_constraintdef(c.oid, true) AS definition
           FROM pg_constraint c
           JOIN pg_class t ON t.oid=c.conrelid
           JOIN pg_namespace n ON n.oid=t.relnamespace
           WHERE n.nspname=COALESCE(%s,current_schema()) AND t.relname=%s
           ORDER BY c.conname""",
        (schema, table), timeout_seconds=None, write=False,
    )
    comments = execute(
        connection,
        db_type,
        """SELECT obj_description(t.oid, 'pg_class') AS table_comment,
                  a.attname AS column_name, col_description(t.oid, a.attnum) AS column_comment
           FROM pg_class t
           JOIN pg_namespace n ON n.oid=t.relnamespace
           LEFT JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum>0 AND NOT a.attisdropped
           WHERE n.nspname=COALESCE(%s,current_schema()) AND t.relname=%s
           ORDER BY a.attnum""",
        (schema, table), timeout_seconds=None, write=False,
    )
    return {"indexes": indexes, "constraints": constraints, "comments": comments}


def ddl(
    connection: Any,
    db_type: str,
    table: str,
    schema: str | None = None,
    *,
    connection_info: dict[str, Any] | None = None,
    password: str | None = None,
) -> str:
    if db_type == "sqlite":
        cursor = connection.execute(
            "SELECT type, sql FROM sqlite_master WHERE tbl_name = ? AND sql IS NOT NULL "
            "AND type IN ('table','index','trigger','view') ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'view' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, name",
            (table,),
        )
        values = [row[1].rstrip(";") + ";" for row in cursor.fetchall()]
        cursor.close()
        if not values:
            raise QueryError(f"对象不存在或无可见 DDL：{table}")
        return "\n\n".join(values)
    if db_type in {"mysql", "mariadb"}:
        cursor = connection.cursor()
        try:
            cursor.execute(f"SHOW CREATE TABLE {quote_identifier(table, db_type)}")
            row = cursor.fetchone()
        finally:
            cursor.close()
        if not row:
            raise QueryError(f"表不存在：{table}")
        return row[1].rstrip(";") + ";"
    if connection_info and shutil.which("pg_dump"):
        info = connection_info

        def quote_pattern(value: str) -> str:
            return '"' + value.replace('"', '""') + '"'

        qualified = f"{quote_pattern(schema or 'public')}.{quote_pattern(table)}"
        command = [
            shutil.which("pg_dump") or "pg_dump", "--schema-only", "--no-owner",
            "--no-privileges", "--strict-names", "--host", str(info["host"]),
            "--port", str(info.get("port", 5432)), "--username", str(info["user"]),
            "--dbname", str(info["database"]), "--table", qualified,
        ]
        environment = os.environ.copy()
        environment.pop("DBQ_MASTER_PASSWORD", None)
        environment.pop("DBQ_MASTER_PASSWORD_FILE", None)
        if password is not None:
            environment["PGPASSWORD"] = password
        environment["PGSSLMODE"] = str(info.get("tls_mode", "verify-full"))
        if info.get("ca_file"):
            environment["PGSSLROOTCERT"] = str(info["ca_file"])
        try:
            completed = subprocess.run(
                command, env=environment, text=True, capture_output=True,
                timeout=info.get("query_timeout") or None, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("pg_dump 获取 DDL 超时") from exc
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip()
        raise QueryError(f"pg_dump 获取 DDL 失败（退出码 {completed.returncode}）")
    # PostgreSQL exposes structured metadata rather than original source. Return a
    # useful reconstruction and label it so callers never mistake it for pg_dump.
    cols = describe(connection, db_type, table, schema)
    qschema = quote_identifier(schema or "public", db_type)
    qtable = quote_identifier(table, db_type)
    definitions = []
    for row in cols.rows:
        _, name, data_type, udt_name, nullable, default, is_identity, identity_generation = row
        type_sql = data_type if data_type not in {"ARRAY", "USER-DEFINED"} else udt_name
        piece = f"  {quote_identifier(name, db_type)} {type_sql}"
        if is_identity == "YES":
            piece += f" GENERATED {identity_generation} AS IDENTITY"
        elif default is not None:
            piece += f" DEFAULT {default}"
        if nullable == "NO":
            piece += " NOT NULL"
        definitions.append(piece)
    if not definitions:
        raise QueryError(f"表不存在或无可见列：{table}")
    return "-- dbq reconstructed DDL; constraints, indexes and comments are reported by inspect\n" + f"CREATE TABLE {qschema}.{qtable} (\n" + ",\n".join(definitions) + "\n);"
