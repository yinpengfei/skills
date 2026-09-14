from __future__ import annotations

import re

from .errors import UsageError


def parse_duration(value: str | int | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(s|m|h)?", text)
    if not match:
        raise UsageError(f"无效时长：{value}；示例：600、10m、1h、0")
    number = float(match.group(1))
    factor = {None: 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]
    return number * factor

