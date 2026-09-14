from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .io import atomic_write_json, directory_lock
from .paths import config_dir

SCHEMA_VERSION = 1
CONNECTION_FIELDS = {
    "type", "host", "port", "database", "user", "path", "charset",
    "connect_timeout", "query_timeout", "tls_mode", "ca_file", "readonly",
    "allow_ddl", "credential_ref",
}


def validate_scope_name(label: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or value in {".", ".."}
        or any(character.isspace() or character in "/\\" or ord(character) < 32 for character in value)
    ):
        raise ConfigError(f"{label} 必须是 1-64 个不含空白、斜线或控制字符的名称")
    return value


def default_config() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "default_project": "default",
        "default_env": "dev",
        "environments": {
            "dev": {"production": False},
            "test": {"production": False},
            "prod": {"production": True},
        },
        "connections": {},
    }


class ConfigStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "config.json"

    def load(self, *, required: bool = True) -> dict[str, Any]:
        if not self.path.exists():
            if required:
                raise ConfigError("dbq 尚未初始化，请先运行 dbq init")
            return default_config()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"配置文件无效：{exc}") from exc
        self.validate(data)
        return data

    def save(self, data: dict[str, Any]) -> None:
        self.validate(data)
        with directory_lock(self.directory):
            atomic_write_json(self.path, data)

    @staticmethod
    def validate(data: object) -> None:
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            raise ConfigError("不支持的配置格式或版本")
        unknown_top = set(data) - {"schema_version", "default_project", "default_env", "environments", "connections"}
        if unknown_top:
            raise ConfigError("配置包含未知字段：" + ", ".join(sorted(unknown_top)))
        if not isinstance(data.get("environments"), dict):
            raise ConfigError("environments 必须是对象")
        if not isinstance(data.get("connections"), dict):
            raise ConfigError("connections 必须是对象")
        default_project = validate_scope_name("default_project", data.get("default_project"))
        default_env = validate_scope_name("default_env", data.get("default_env"))
        for env, settings in data["environments"].items():
            validate_scope_name("环境名", env)
            if not isinstance(settings, dict) or set(settings) != {"production"} or type(settings["production"]) is not bool:
                raise ConfigError(f"环境配置无效：{env}")
        if default_env not in data["environments"]:
            raise ConfigError(f"默认环境不存在：{default_env}")
        for project, environments in data["connections"].items():
            validate_scope_name("项目名", project)
            if not isinstance(environments, dict):
                raise ConfigError(f"项目连接配置无效：{project}")
            for env, connections in environments.items():
                validate_scope_name("环境名", env)
                if env not in data["environments"] or not isinstance(connections, dict):
                    raise ConfigError(f"连接环境无效：{project}/{env}")
                for name, info in connections.items():
                    validate_scope_name("连接名", name)
                    ConfigStore._validate_connection(project, env, name, info)
    @staticmethod
    def _validate_connection(project: str, env: str, name: str, info: object) -> None:
        target = f"{project}/{env}/{name}"
        if not isinstance(info, dict):
            raise ConfigError(f"连接配置必须是对象：{target}")
        unknown = set(info) - CONNECTION_FIELDS
        if unknown:
            raise ConfigError(f"连接包含未知字段 {target}：" + ", ".join(sorted(unknown)))
        db_type = info.get("type")
        if db_type not in {"mysql", "mariadb", "postgresql", "sqlite"}:
            raise ConfigError(f"连接类型无效：{target}")
        for field in ("readonly", "allow_ddl"):
            if type(info.get(field)) is not bool:
                raise ConfigError(f"连接 {target} 的 {field} 必须是布尔值")
        for field in ("host", "database", "user", "path", "charset", "tls_mode", "ca_file", "credential_ref"):
            if field in info and not isinstance(info[field], str):
                raise ConfigError(f"连接 {target} 的 {field} 必须是字符串")
        if "port" in info and (type(info["port"]) is not int or not 1 <= info["port"] <= 65535):
            raise ConfigError(f"连接 {target} 的 port 无效")
        for field in ("connect_timeout", "query_timeout"):
            if field in info and (type(info[field]) not in {int, float} or info[field] < 0):
                raise ConfigError(f"连接 {target} 的 {field} 无效")
        if db_type == "sqlite":
            if not info.get("path"):
                raise ConfigError(f"SQLite 连接缺少 path：{target}")
            if "credential_ref" in info:
                raise ConfigError(f"SQLite 连接不能包含凭据引用：{target}")
        else:
            if any(not info.get(field) for field in ("host", "database", "user")):
                raise ConfigError(f"网络连接缺少 host、database 或 user：{target}")
            if info.get("tls_mode", "verify-full") not in {"disable", "require", "verify-ca", "verify-full"}:
                raise ConfigError(f"连接 TLS 模式无效：{target}")
            if "credential_ref" in info and info["credential_ref"] != target:
                raise ConfigError(f"连接凭据引用与目标不一致：{target}")

    def initialize(self) -> dict[str, Any]:
        if self.path.exists():
            raise ConfigError("dbq 已初始化，不会覆盖现有配置")
        data = default_config()
        self.save(data)
        return data

    def environment(self, data: dict[str, Any], name: str) -> dict[str, Any]:
        validate_scope_name("环境名", name)
        try:
            value = data["environments"][name]
        except KeyError as exc:
            raise ConfigError(f"环境不存在：{name}") from exc
        if not isinstance(value, dict):
            raise ConfigError(f"环境配置无效：{name}")
        return value

    def get_connection(self, project: str, env: str, name: str) -> dict[str, Any]:
        validate_scope_name("项目名", project)
        validate_scope_name("连接名", name)
        data = self.load()
        self.environment(data, env)
        try:
            result = data["connections"][project][env][name]
        except KeyError as exc:
            raise ConfigError(f"连接不存在：{project}/{env}/{name}") from exc
        return deepcopy(result)

    def put_connection(
        self, project: str, env: str, name: str, value: dict[str, Any], *, replace: bool = False
    ) -> None:
        validate_scope_name("项目名", project)
        validate_scope_name("连接名", name)
        data = self.load()
        self.environment(data, env)
        env_map = data["connections"].setdefault(project, {}).setdefault(env, {})
        if name in env_map and not replace:
            raise ConfigError(f"连接已存在：{project}/{env}/{name}；请使用 conn edit")
        env_map[name] = deepcopy(value)
        self.save(data)

    def remove_connection(self, project: str, env: str, name: str) -> dict[str, Any]:
        validate_scope_name("项目名", project)
        validate_scope_name("环境名", env)
        validate_scope_name("连接名", name)
        data = self.load()
        try:
            removed = data["connections"][project][env].pop(name)
        except KeyError as exc:
            raise ConfigError(f"连接不存在：{project}/{env}/{name}") from exc
        self.save(data)
        return removed


def open_store(override: str | None = None) -> ConfigStore:
    return ConfigStore(config_dir(override))
