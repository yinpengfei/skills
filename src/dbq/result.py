from __future__ import annotations

import base64
import datetime as dt
import decimal
import json
from dataclasses import dataclass, field
from typing import Any


def encode_value(value: Any) -> Any:
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 9_007_199_254_740_991:
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): encode_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_value(v) for v in value]
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"encoding": "base64", "data": base64.b64encode(bytes(value)).decode("ascii")}
    return str(value)


@dataclass
class QueryResult:
    columns: list[dict[str, Any]]
    rows: list[list[Any]]
    row_count: int
    elapsed_ms: int
    truncated: bool = False
    affected_rows: int | None = None
    warnings: list[str] = field(default_factory=list)

    def document(self, *, target: dict[str, str]) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": "ok",
            "target": target,
            "columns": self.columns,
            "rows": [[encode_value(value) for value in row] for row in self.rows],
            "row_count": self.row_count,
            "affected_rows": self.affected_rows,
            "truncated": self.truncated,
            "elapsed_ms": self.elapsed_ms,
            "warnings": self.warnings,
        }


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def enforce_preview_budget(result: QueryResult, budget: int = 10 * 1024 * 1024) -> None:
    used = 0
    kept: list[list[Any]] = []
    for row in result.rows:
        compact = []
        for value in row:
            if isinstance(value, str) and len(value.encode("utf-8")) > 65536:
                value = value.encode("utf-8")[:65536].decode("utf-8", errors="ignore") + "…[field truncated]"
            elif isinstance(value, (bytes, bytearray, memoryview)) and len(value) > 4096:
                value = {"encoding": "binary-summary", "bytes": len(value)}
            compact.append(value)
        encoded_size = len(json.dumps([encode_value(v) for v in compact], ensure_ascii=False).encode("utf-8"))
        if used + encoded_size > budget:
            result.truncated = True
            result.warnings.append("preview output exceeded 10 MiB byte budget")
            break
        kept.append(compact)
        used += encoded_size
    result.rows = kept
    result.row_count = len(kept)
