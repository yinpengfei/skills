from __future__ import annotations

import base64
import getpass
import json
import os
import re
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

from .errors import ConfigError, UsageError
from .io import atomic_write_json, directory_lock

SERVICE = "dbq-cli"
AAD_DEK = b"dbq/vault/dek/v1"
AAD_PAYLOAD = b"dbq/vault/payload/v1"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value.encode("ascii"))
    except Exception as exc:
        raise ConfigError("凭据库包含无效编码") from exc


def _derive(password: str, salt: bytes, params: dict[str, int]) -> bytes:
    if not password:
        raise ConfigError("主密码不能为空")
    return Argon2id(
        salt=salt,
        length=32,
        iterations=params["iterations"],
        lanes=params["lanes"],
        memory_cost=params["memory_cost_kib"],
    ).derive(password.encode("utf-8"))


def read_master_from_environment() -> str | None:
    inline = os.environ.get("DBQ_MASTER_PASSWORD")
    file_name = os.environ.get("DBQ_MASTER_PASSWORD_FILE")
    if inline is not None and file_name is not None:
        raise ConfigError("DBQ_MASTER_PASSWORD 与 DBQ_MASTER_PASSWORD_FILE 不能同时设置")
    if file_name is not None:
        path = Path(file_name).expanduser()
        try:
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 65536:
                raise ConfigError("DBQ_MASTER_PASSWORD_FILE 必须是小型普通文件")
            if os.name != "nt" and metadata.st_mode & 0o077:
                raise ConfigError("DBQ_MASTER_PASSWORD_FILE 权限过宽，请设置为仅当前用户可读")
            value = path.read_text(encoding="utf-8").splitlines()[0]
        except ConfigError:
            raise
        except (OSError, IndexError) as exc:
            raise ConfigError("无法读取 DBQ_MASTER_PASSWORD_FILE") from exc
        return value
    return inline


def _account(vault_id: str) -> str:
    return f"vault-unlock-key:{vault_id}"


def _keyring_get(vault_id: str) -> bytes | None:
    try:
        import keyring

        value = keyring.get_password(SERVICE, _account(vault_id))
    except Exception:
        return None
    if not value:
        return None
    raw = _unb64(value)
    return raw if len(raw) == 32 else None


def _keyring_set(vault_id: str, key: bytes) -> None:
    try:
        import keyring

        keyring.set_password(SERVICE, _account(vault_id), _b64(key))
    except Exception as exc:
        raise ConfigError(
            "系统凭据库不可用；可使用 DBQ_MASTER_PASSWORD_FILE 自动解锁"
        ) from exc


def _keyring_delete(vault_id: str) -> None:
    try:
        import keyring

        keyring.delete_password(SERVICE, _account(vault_id))
    except Exception:
        return


@dataclass
class UnlockedVault:
    vault: "Vault"
    unlock_key: bytes
    dek: bytes
    credentials: dict[str, str]

    def save(self) -> None:
        self.vault._write(self.unlock_key, self.dek, self.credentials)

    def get(self, ref: str) -> str | None:
        return self.credentials.get(ref)

    def set(self, ref: str, password: str) -> None:
        self.credentials[ref] = password
        self.save()

    def delete(self, ref: str) -> None:
        if ref in self.credentials:
            del self.credentials[ref]
            self.save()


class Vault:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "vault.dbq"

    def exists(self) -> bool:
        return self.path.exists()

    def initialize(self, master_password: str, *, remember: bool = True) -> None:
        if self.path.exists():
            raise ConfigError("凭据库已存在，不会覆盖")
        salt = secrets.token_bytes(16)
        params = {"iterations": 3, "lanes": 4, "memory_cost_kib": 65536}
        unlock_key = _derive(master_password, salt, params)
        dek = secrets.token_bytes(32)
        vault_id = secrets.token_hex(16)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._write(unlock_key, dek, {}, salt=salt, params=params, vault_id=vault_id)
        if remember:
            _keyring_set(vault_id, unlock_key)

    def _read_document(self) -> dict[str, Any]:
        if not self.path.exists():
            raise ConfigError("凭据库不存在，请先运行 dbq init")
        try:
            if self.path.stat().st_size > 16 * 1024 * 1024:
                raise ConfigError("凭据库超过安全大小限制")
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except ConfigError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError("凭据库损坏或不可读") from exc
        if doc.get("schema_version") != 1 or doc.get("cipher") != "AES-256-GCM":
            raise ConfigError("不支持的凭据库格式")
        if not isinstance(doc.get("vault_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", doc["vault_id"]):
            raise ConfigError("凭据库 ID 无效")
        params = doc.get("kdf", {})
        if params.get("name") != "argon2id":
            raise ConfigError("不支持的密钥派生算法")
        memory = params.get("memory_cost_kib")
        iterations = params.get("iterations")
        lanes = params.get("lanes")
        if not all(isinstance(v, int) for v in (memory, iterations, lanes)):
            raise ConfigError("凭据库 KDF 参数无效")
        if not (8192 <= memory <= 262144 and 1 <= iterations <= 10 and 1 <= lanes <= 16):
            raise ConfigError("凭据库 KDF 参数超出安全范围")
        return doc

    def unlock(self, master_password: str | None = None) -> UnlockedVault:
        doc = self._read_document()
        params = doc["kdf"]
        key = None
        if master_password is None:
            env_value = read_master_from_environment()
            if env_value is not None:
                key = _derive(env_value, _unb64(params["salt"]), params)
            else:
                key = _keyring_get(doc["vault_id"])
        else:
            key = _derive(master_password, _unb64(params["salt"]), params)
        if key is None:
            raise ConfigError("凭据库未解锁；运行 dbq vault unlock --remember")
        try:
            wrapped = doc["wrapped_dek"]
            dek = AESGCM(key).decrypt(
                _unb64(wrapped["nonce"]), _unb64(wrapped["ciphertext"]), AAD_DEK
            )
            payload = doc["payload"]
            clear = AESGCM(dek).decrypt(
                _unb64(payload["nonce"]), _unb64(payload["ciphertext"]), AAD_PAYLOAD
            )
            values = json.loads(clear.decode("utf-8"))
        except (InvalidTag, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ConfigError("主密码错误或凭据库已损坏") from exc
        if not isinstance(values, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in values.items()
        ):
            raise ConfigError("凭据库内容无效")
        return UnlockedVault(self, key, dek, values)

    def _write(
        self,
        unlock_key: bytes,
        dek: bytes,
        credentials: dict[str, str],
        *,
        salt: bytes | None = None,
        params: dict[str, int] | None = None,
        vault_id: str | None = None,
    ) -> None:
        if salt is None or params is None:
            old = self._read_document()
            params = {
                "iterations": old["kdf"]["iterations"],
                "lanes": old["kdf"]["lanes"],
                "memory_cost_kib": old["kdf"]["memory_cost_kib"],
            }
            salt = _unb64(old["kdf"]["salt"])
            vault_id = old["vault_id"]
        if vault_id is None:
            raise ConfigError("缺少凭据库 ID")
        dek_nonce = secrets.token_bytes(12)
        data_nonce = secrets.token_bytes(12)
        clear = json.dumps(credentials, ensure_ascii=False, sort_keys=True).encode("utf-8")
        doc = {
            "schema_version": 1,
            "vault_id": vault_id,
            "cipher": "AES-256-GCM",
            "kdf": {"name": "argon2id", "salt": _b64(salt), **params},
            "wrapped_dek": {
                "nonce": _b64(dek_nonce),
                "ciphertext": _b64(AESGCM(unlock_key).encrypt(dek_nonce, dek, AAD_DEK)),
            },
            "payload": {
                "nonce": _b64(data_nonce),
                "ciphertext": _b64(AESGCM(dek).encrypt(data_nonce, clear, AAD_PAYLOAD)),
            },
        }
        with directory_lock(self.directory):
            atomic_write_json(self.path, doc)

    def remember(self, master_password: str) -> None:
        unlocked = self.unlock(master_password)
        _keyring_set(self._read_document()["vault_id"], unlocked.unlock_key)

    def forget(self) -> None:
        _keyring_delete(self._read_document()["vault_id"])

    def change_password(self, old_password: str, new_password: str, *, remember: bool) -> None:
        unlocked = self.unlock(old_password)
        old = self._read_document()
        salt = secrets.token_bytes(16)
        params = {
            "iterations": old["kdf"]["iterations"],
            "lanes": old["kdf"]["lanes"],
            "memory_cost_kib": old["kdf"]["memory_cost_kib"],
        }
        vault_id = old["vault_id"]
        key = _derive(new_password, salt, params)
        self._write(key, unlocked.dek, unlocked.credentials, salt=salt, params=params, vault_id=vault_id)
        _keyring_delete(vault_id)
        if remember:
            _keyring_set(vault_id, key)


def credential_ref(project: str, env: str, name: str) -> str:
    return f"{project}/{env}/{name}"


def prompt_new_master(mode: str, *, non_interactive: bool) -> str:
    if mode == "generate":
        if non_interactive or not sys.stdin.isatty() or not sys.stderr.isatty():
            raise UsageError("随机主密码只能在真实交互终端生成并显示")
        value = secrets.token_urlsafe(24)
        print(f"生成的主密码（请立即安全保存）：{value}", file=sys.stderr)
        return value
    if non_interactive:
        value = read_master_from_environment()
        if value is None:
            raise UsageError("非交互初始化需要 DBQ_MASTER_PASSWORD 或密码文件")
        return value
    first = getpass.getpass("设置 dbq 主密码：")
    second = getpass.getpass("再次输入主密码：")
    if first != second:
        raise UsageError("两次输入的主密码不一致")
    if len(first) < 12:
        raise UsageError("主密码至少需要 12 个字符")
    return first
