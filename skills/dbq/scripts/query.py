#!/usr/bin/env python3
"""Compatibility entry point for one transition release."""

from __future__ import annotations

import sys

try:
    from dbq.cli import main
except ImportError:
    print(
        "dbq 已重构为可安装命令。请在仓库根目录运行 "
        "'uv tool install .'，然后使用 'dbq --help'。",
        file=sys.stderr,
    )
    raise SystemExit(3)

raise SystemExit(main())
