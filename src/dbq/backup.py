from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

from .errors import BackupError
from .vault import Vault


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def create_backup(
    source: Path,
    destination: Path,
    *,
    unlocked=None,
    backup_password: str | None = None,
) -> None:
    config_path = source / "config.json"
    vault_path = source / "vault.dbq"
    if not config_path.exists() or not vault_path.exists():
        raise BackupError("配置或凭据库不存在，无法备份")
    if destination.exists():
        raise BackupError(f"备份文件已存在：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dbq-backup-") as temp_name:
        temp = Path(temp_name)
        shutil.copy2(config_path, temp / "config.json")
        if backup_password is None:
            shutil.copy2(vault_path, temp / "vault.dbq")
        else:
            if unlocked is None:
                raise BackupError("独立口令备份需要先解锁当前凭据库")
            target_vault = Vault(temp)
            target_vault.initialize(backup_password, remember=False)
            target = target_vault.unlock(backup_password)
            target.credentials.update(unlocked.credentials)
            target.save()
        files = {}
        for name in ("config.json", "vault.dbq"):
            files[name] = _digest((temp / name).read_bytes())
        manifest = {
            "schema_version": 1,
            "kind": "dbq-config-backup",
            "rekeyed": backup_password is not None,
            "files": files,
        }
        (temp / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        try:
            import os

            fd = os.open(destination, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w+b") as stream:
                with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for name in ("manifest.json", "config.json", "vault.dbq"):
                        archive.write(temp / name, name)
        except Exception as exc:
            destination.unlink(missing_ok=True)
            raise BackupError(f"创建备份失败：{type(exc).__name__}") from exc


def verify_backup(path: Path, *, master_password: str | None = None) -> dict:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = set(archive.namelist())
            required = {"manifest.json", "config.json", "vault.dbq"}
            if names != required:
                raise BackupError("备份内容不完整或包含未知文件")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("schema_version") != 1 or manifest.get("kind") != "dbq-config-backup":
                raise BackupError("不支持的备份格式")
            for name in ("config.json", "vault.dbq"):
                value = archive.read(name)
                if _digest(value) != manifest.get("files", {}).get(name):
                    raise BackupError(f"备份校验失败：{name}")
            json.loads(archive.read("config.json"))
            json.loads(archive.read("vault.dbq"))
            with tempfile.TemporaryDirectory(prefix="dbq-verify-") as temp_name:
                temp = Path(temp_name)
                (temp / "vault.dbq").write_bytes(archive.read("vault.dbq"))
                Vault(temp).unlock(master_password)
            return manifest
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(f"无法验证备份：{type(exc).__name__}") from exc


def restore_backup(
    path: Path, destination: Path, *, dry_run: bool = False, master_password: str | None = None
) -> dict:
    manifest = verify_backup(path, master_password=master_password)
    conflicts = [name for name in ("config.json", "vault.dbq") if (destination / name).exists()]
    if conflicts:
        raise BackupError("恢复目标已有配置，不会覆盖：" + ", ".join(conflicts))
    if dry_run:
        return {"destination": str(destination), "files": ["config.json", "vault.dbq"]}
    destination.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    try:
        with zipfile.ZipFile(path, "r") as archive:
            for name in ("config.json", "vault.dbq"):
                target = destination / name
                temporary = destination / f".{name}.restore"
                temporary.write_bytes(archive.read(name))
                temporary.replace(target)
                created.append(target)
                if target.name == "vault.dbq":
                    target.chmod(0o600)
    except Exception as exc:
        for target in created:
            target.unlink(missing_ok=True)
        raise BackupError(f"恢复失败：{type(exc).__name__}") from exc
    return manifest
