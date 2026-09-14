from __future__ import annotations

import os
import sys
from pathlib import Path


def config_dir(override: str | os.PathLike[str] | None = None) -> Path:
    """Return a portable config directory without creating it."""
    if override:
        return Path(override).expanduser().resolve()
    if value := os.environ.get("DBQ_CONFIG_DIR"):
        return Path(value).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "dbq"
    if value := os.environ.get("XDG_CONFIG_HOME"):
        return Path(value).expanduser() / "dbq"
    return Path.home() / ".config" / "dbq"
