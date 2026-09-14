from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .config import ConfigStore
from .errors import ConfigError
from .vault import credential_ref

PLACEHOLDER = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _files(source: Path) -> list[Path]:
    if source.is_file():
        return [source]
    if source.is_dir():
        return sorted(source.glob("connections*.yaml")) + sorted(source.glob("connections*.yml"))
    raise ConfigError(f"旧配置来源不存在：{source}")


def _environment(path: Path) -> str:
    match = re.match(r"connections\.([^.]+)\.ya?ml$", path.name)
    return match.group(1) if match else "dev"


def _dotenv(source: Path) -> dict[str, str]:
    path = source / ".env" if source.is_dir() else source.parent / ".env"
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError as exc:
        raise ConfigError("无法读取旧 .env") from exc
    return values


def _load(source: Path) -> list[tuple[str, str, dict[str, Any]]]:
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError("迁移旧 YAML 需要 PyYAML，请重新安装完整 dbq") from exc
    records = []
    for path in _files(source):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise ConfigError(f"旧配置无法解析：{path.name}") from exc
        connections = data.get("connections", {}) if isinstance(data, dict) else {}
        if not isinstance(connections, dict):
            raise ConfigError(f"旧配置 connections 无效：{path.name}")
        env = _environment(path)
        for alias, info in connections.items():
            if isinstance(alias, str) and isinstance(info, dict):
                records.append((env, alias, dict(info)))
    if not records:
        raise ConfigError("旧配置中没有可迁移连接")
    return records


def preview(source: Path) -> list[dict[str, Any]]:
    result = []
    for env, alias, info in _load(source):
        result.append(
            {
                "environment": env,
                "name": alias,
                "type": info.get("type"),
                "credential": "present-in-source" if info.get("password") else "lookup-required",
                "permissions": "reset-to-readonly",
            }
        )
    return result


def _legacy_password(source: Path, env: str, alias: str, info: dict[str, Any], dotenv: dict[str, str]) -> str | None:
    value = info.get("password")
    if isinstance(value, str):
        match = PLACEHOLDER.fullmatch(value.strip())
        if match:
            key = match.group(1)
            return os.environ.get(key, dotenv.get(key))
        return value
    conventional = re.sub(r"[^A-Z0-9_]", "_", f"DB_PWD_{env}_{alias}".upper())
    if conventional in os.environ or conventional in dotenv:
        return os.environ.get(conventional, dotenv.get(conventional))
    try:
        import keyring

        return keyring.get_password(f"dbq/{env}/{alias}", "dbq")
    except Exception:
        return None


def migrate(source: Path, store: ConfigStore, unlocked) -> list[dict[str, Any]]:
    records = _load(source)
    data = store.load()
    dotenv = _dotenv(source)
    planned = []
    seen = set()
    for env, alias, old in records:
        key = (env, alias)
        if key in seen:
            raise ConfigError(f"旧配置存在重复连接：{env}/{alias}")
        seen.add(key)
        if alias in data.get("connections", {}).get("default", {}).get(env, {}):
            raise ConfigError(f"目标连接已存在，不会覆盖：default/{env}/{alias}")
        if env not in data["environments"]:
            data["environments"][env] = {"production": env.lower() in {"prod", "production"}}
        allowed = {
            "type", "host", "port", "user", "database", "charset", "connect_timeout",
            "query_timeout", "path", "ca_file",
        }
        info = {k: v for k, v in old.items() if k in allowed}
        if info.get("type") != "sqlite":
            legacy_tls = old.get("tls_mode", old.get("sslmode"))
            info["tls_mode"] = (
                legacy_tls
                if legacy_tls in {"verify-ca", "verify-full"}
                else "verify-full"
            )
        info["readonly"] = True
        info["allow_ddl"] = False
        password = _legacy_password(source, env, alias, old, dotenv)
        ref = credential_ref("default", env, alias)
        if password is not None:
            info["credential_ref"] = ref
        data["connections"].setdefault("default", {}).setdefault(env, {})[alias] = info
        planned.append((env, alias, info, password, ref))
    old_credentials = dict(unlocked.credentials)
    new_credentials = dict(old_credentials)
    for _, _, _, password, ref in planned:
        if password is not None:
            new_credentials[ref] = password
    unlocked.credentials = new_credentials
    unlocked.save()
    try:
        store.save(data)
    except Exception:
        unlocked.credentials = old_credentials
        unlocked.save()
        raise
    return [
        {
            "environment": env,
            "name": alias,
            "credential": "migrated" if password is not None else "missing",
            "permissions": "readonly",
        }
        for env, alias, _, password, _ in planned
    ]
