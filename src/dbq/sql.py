from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import PolicyError, UsageError


@dataclass(frozen=True)
class SqlAnalysis:
    sql: str
    kind: str
    dialect: str
    ast: object | None = None


def split_statements(sql: str) -> list[str]:
    """Split SQL while preserving semicolons in strings, identifiers and comments."""
    statements: list[str] = []
    start = 0
    i = 0
    state = "plain"
    dollar_tag = ""
    while i < len(sql):
        c = sql[i]
        n = sql[i + 1] if i + 1 < len(sql) else ""
        if state == "plain":
            if c == "'":
                state = "single"
            elif c == '"':
                state = "double"
            elif c == "`":
                state = "backtick"
            elif c == "[":
                state = "bracket"
            elif c == "-" and n == "-":
                state = "line_comment"
                i += 1
            elif c == "/" and n == "*":
                state = "block_comment"
                i += 1
            elif c == "$":
                match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql[i:])
                if match:
                    dollar_tag = match.group(0)
                    state = "dollar"
                    i += len(dollar_tag) - 1
            elif c == ";":
                piece = sql[start:i].strip()
                if piece:
                    statements.append(piece)
                start = i + 1
        elif state == "single":
            if c == "'":
                if n == "'":
                    i += 1
                else:
                    state = "plain"
            elif c == "\\" and n:
                i += 1
        elif state == "double":
            if c == '"':
                if n == '"':
                    i += 1
                else:
                    state = "plain"
        elif state == "backtick":
            if c == "`":
                if n == "`":
                    i += 1
                else:
                    state = "plain"
        elif state == "bracket" and c == "]":
            state = "plain"
        elif state == "line_comment" and c in "\r\n":
            state = "plain"
        elif state == "block_comment" and c == "*" and n == "/":
            state = "plain"
            i += 1
        elif state == "dollar" and sql.startswith(dollar_tag, i):
            i += len(dollar_tag) - 1
            state = "plain"
        i += 1
    if state in {"single", "double", "backtick", "bracket", "block_comment", "dollar"}:
        raise UsageError("SQL 包含未闭合的字符串、标识符或注释")
    tail = sql[start:].strip()
    if tail:
        statements.append(tail)
    return statements


def _leading_tokens(sql: str) -> list[str]:
    value = re.sub(r"\A(?:\s|--[^\r\n]*(?:\r?\n|$)|/\*.*?\*/)*", "", sql, flags=re.S)
    return re.findall(r"[A-Za-z_]+", value[:160].upper())


def _fallback_kind(sql: str) -> str:
    tokens = _leading_tokens(sql)
    if not tokens:
        raise UsageError("SQL 为空")
    first = tokens[0]
    if first == "WITH":
        raise PolicyError("当前环境缺少 SQLGlot，无法安全分析 WITH；请修复 dbq 安装")
    if first == "EXPLAIN":
        if "ANALYZE" in tokens[:4]:
            raise PolicyError("不允许执行型 EXPLAIN ANALYZE")
        return "READ"
    if first in {"SELECT", "SHOW", "DESCRIBE", "DESC", "VALUES"}:
        return "READ"
    if first in {"INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE"}:
        return "DML"
    if first in {"CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME"}:
        return "DDL"
    return "BLOCKED"


def analyze(sql: str, dialect: str, *, allow_multi: bool = False) -> list[SqlAnalysis]:
    parts = split_statements(sql)
    if not parts:
        raise UsageError("没有有效 SQL")
    if len(parts) > 1 and not allow_multi:
        raise PolicyError("默认只允许单条 SQL；只读批量查询请显式使用 --multi")
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        results = [SqlAnalysis(p, _fallback_kind(p), dialect) for p in parts]
    else:
        results = []
        for part in parts:
            try:
                parsed = sqlglot.parse(part, read=dialect)
            except Exception as exc:
                raise PolicyError(f"SQL 无法按 {dialect} 方言安全解析：{exc}") from exc
            if len(parsed) != 1 or parsed[0] is None:
                raise PolicyError("SQL 必须恰好包含一条可解析语句")
            root = parsed[0]
            root_name = type(root).__name__
            node_names = {type(node).__name__ for node in root.walk()}
            if node_names & {"Insert", "Update", "Delete", "Merge"}:
                kind = "DML"
            elif root_name in {"Create", "Alter", "AlterTable", "Drop", "TruncateTable", "RenameTable"}:
                kind = "DDL"
            elif isinstance(root, exp.Query):
                kind = "READ"
            elif root_name in {"Show", "Describe", "Command"} and _leading_tokens(part)[0] in {
                "SHOW", "DESCRIBE", "DESC", "EXPLAIN"
            }:
                kind = "READ"
            else:
                kind = "BLOCKED"
            upper = " ".join(_leading_tokens(part)[:4])
            if upper.startswith("EXPLAIN") and "ANALYZE" in upper:
                raise PolicyError("不允许执行型 EXPLAIN ANALYZE")
            if node_names & {"Into", "Lock", "Copy"}:
                kind = "BLOCKED"
            results.append(SqlAnalysis(part, kind, dialect, root))
    if allow_multi and any(item.kind != "READ" for item in results):
        raise PolicyError("--multi 只允许只读语句")
    return results


def ensure_write_policy(item: SqlAnalysis, connection: dict, *, execute: bool) -> None:
    if item.kind == "BLOCKED":
        raise PolicyError("该 SQL 类型不受支持")
    if execute:
        if item.kind == "READ":
            raise PolicyError("exec 只用于写语句；查询请使用 query")
        if item.kind == "DML" and connection.get("readonly", True):
            raise PolicyError("该连接默认只读，未开启 DML")
        if item.kind == "DDL" and not connection.get("allow_ddl", False):
            raise PolicyError("该连接未开启执行 DDL")
        _ensure_top_level_where(item)
    elif item.kind != "READ":
        raise PolicyError("query 只允许只读 SQL；写操作请使用 exec")


def _ensure_top_level_where(item: SqlAnalysis) -> None:
    try:
        from sqlglot import exp
    except ImportError:
        first = _leading_tokens(item.sql)[0]
        if first in {"UPDATE", "DELETE"}:
            raise PolicyError("缺少 SQLGlot 时拒绝 UPDATE/DELETE，无法可靠验证顶层 WHERE")
        return
    if isinstance(item.ast, (exp.Update, exp.Delete)):
        where = item.ast.args.get("where")
        if where is None:
            raise PolicyError("拒绝没有顶层 WHERE 的 UPDATE/DELETE")
        condition = where.this
        if isinstance(condition, exp.Boolean) and condition.this is True:
            raise PolicyError("拒绝恒真的整表 UPDATE/DELETE 条件")
        if isinstance(condition, exp.EQ):
            left, right = condition.this, condition.expression
            if isinstance(left, exp.Literal) and isinstance(right, exp.Literal) and left.this == right.this:
                raise PolicyError("拒绝恒真的整表 UPDATE/DELETE 条件")


def apply_preview_limit(item: SqlAnalysis, limit: int | None) -> tuple[str, bool]:
    if limit is None:
        return item.sql, False
    if limit <= 0:
        raise UsageError("limit 必须大于 0")
    if item.ast is None:
        if _leading_tokens(item.sql)[0] != "SELECT":
            return item.sql, False
        if re.search(r"\bLIMIT\b|\bFETCH\s+(?:FIRST|NEXT)\b", item.sql, re.I):
            return item.sql, False
        return item.sql.rstrip().rstrip(";") + f" LIMIT {limit}", True
    try:
        from sqlglot import exp
    except ImportError:
        return item.sql, False
    if not isinstance(item.ast, exp.Query) or item.ast.args.get("limit") is not None:
        return item.sql, False
    clone = item.ast.copy().limit(limit)
    return clone.sql(dialect=item.dialect), True


def count_sql(item: SqlAnalysis) -> str:
    if item.kind != "READ":
        raise PolicyError("count 只支持只读查询")
    clean = item.sql.rstrip().rstrip(";")
    return f"SELECT COUNT(*) AS dbq_count FROM ({clean}) AS dbq_count_source"
