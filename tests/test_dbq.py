from __future__ import annotations

import contextlib
import io
import json
import importlib.util
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dbq.cli import main
from dbq.config import ConfigStore
from dbq.duration import parse_duration
from dbq.errors import ConfigError, PolicyError, UnknownWriteResultError, UsageError
from dbq.sql import analyze, ensure_write_policy, split_statements
from dbq.vault import Vault, read_master_from_environment
from dbq.migrate import migrate, preview
from dbq.paths import config_dir
from dbq.drivers import connect


class DbqTest(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(parse_duration("600"), 600)
        self.assertEqual(parse_duration("10m"), 600)
        self.assertEqual(parse_duration("1h"), 3600)
        self.assertEqual(parse_duration("0"), 0)
        with self.assertRaises(UsageError):
            parse_duration("tomorrow")

    def test_windows_config_path_uses_appdata(self):
        with mock.patch("dbq.paths.sys.platform", "win32"), mock.patch.dict(
            os.environ, {"APPDATA": r"C:\\Users\\Ada\\AppData\\Roaming"}, clear=True
        ):
            self.assertEqual(
                str(config_dir()),
                str(Path(r"C:\\Users\\Ada\\AppData\\Roaming") / "dbq"),
            )

    def test_config_rejects_string_permissions_and_credential_aliasing(self):
        with tempfile.TemporaryDirectory() as name:
            store = ConfigStore(Path(name))
            data = store.initialize()
            invalid = dict(data)
            invalid["connections"] = {
                "default": {"dev": {"app": {
                    "type": "mysql", "host": "db.example", "database": "app", "user": "reader",
                    "readonly": True, "allow_ddl": "false", "credential_ref": "default/dev/other",
                }}}
            }
            with self.assertRaises(ConfigError):
                store.save(invalid)

    def test_split_preserves_semicolons(self):
        sql = "SELECT 'a;b'; -- ; ignored\n SELECT $$c;d$$; /* ; */ SELECT 3"
        self.assertEqual(len(split_statements(sql)), 3)
        with self.assertRaises(UsageError):
            split_statements("SELECT 'open")

    def test_guard_rejects_mixed_and_explain_analyze(self):
        with self.assertRaises(PolicyError):
            analyze("SELECT 1; DELETE FROM users", "sqlite")
        with self.assertRaises(PolicyError):
            analyze("EXPLAIN ANALYZE DELETE FROM users", "postgres")
        with self.assertRaises(PolicyError):
            item = analyze("PRAGMA writable_schema=ON", "sqlite")[0]
            ensure_write_policy(item, {"readonly": True}, execute=False)

    @unittest.skipUnless(importlib.util.find_spec("sqlglot"), "SQLGlot dependency not installed")
    def test_ast_handles_cte_comments_and_limit(self):
        from dbq.sql import apply_preview_limit

        read = analyze("WITH x AS (SELECT 1 AS n) SELECT * FROM x", "postgres")[0]
        self.assertEqual(read.kind, "READ")
        write = analyze("/* comment */ WITH x AS (SELECT 1) DELETE FROM users WHERE id IN (SELECT * FROM x)", "postgres")[0]
        self.assertEqual(write.kind, "DML")
        limited, injected = apply_preview_limit(analyze("SELECT * FROM users OFFSET 10", "postgres")[0], 100)
        self.assertTrue(injected)
        self.assertIn("LIMIT 100 OFFSET 10", limited)

    def test_vault_round_trip_change_and_tamper(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            vault = Vault(directory)
            vault.initialize("correct horse battery staple", remember=False)
            unlocked = vault.unlock("correct horse battery staple")
            unlocked.set("default/dev/app", "secret-value")
            self.assertEqual(vault.unlock("correct horse battery staple").get("default/dev/app"), "secret-value")
            self.assertNotIn("secret-value", vault.path.read_text(encoding="utf-8"))
            with mock.patch.dict(os.environ, {"DBQ_MASTER_PASSWORD": ""}, clear=True), mock.patch(
                "dbq.vault._keyring_get", return_value=unlocked.unlock_key
            ) as keyring_get:
                with self.assertRaises(ConfigError):
                    vault.unlock()
                keyring_get.assert_not_called()
            vault.change_password("correct horse battery staple", "a different strong password", remember=False)
            with self.assertRaises(ConfigError):
                vault.unlock("correct horse battery staple")
            self.assertEqual(vault.unlock("a different strong password").get("default/dev/app"), "secret-value")
            document = json.loads(vault.path.read_text(encoding="utf-8"))
            document["payload"]["ciphertext"] = document["payload"]["ciphertext"][:-2] + "AA"
            vault.path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(ConfigError):
                vault.unlock("a different strong password")

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not authoritative on Windows")
    def test_master_password_file_requires_private_permissions(self):
        with tempfile.TemporaryDirectory() as name:
            password_file = Path(name) / "master"
            password_file.write_text("correct horse battery staple\n", encoding="utf-8")
            password_file.chmod(0o644)
            with mock.patch.dict(os.environ, {"DBQ_MASTER_PASSWORD_FILE": str(password_file)}, clear=True):
                with self.assertRaises(ConfigError):
                    read_master_from_environment()
            password_file.chmod(0o600)
            with mock.patch.dict(os.environ, {"DBQ_MASTER_PASSWORD_FILE": str(password_file)}, clear=True):
                self.assertEqual(read_master_from_environment(), "correct horse battery staple")

    def test_connect_preserves_unknown_write_result_signal(self):
        fake = mock.MagicMock()
        with mock.patch("dbq.drivers._sqlite_path", return_value=(":memory:", False)), mock.patch(
            "dbq.drivers.sqlite3.connect", return_value=fake
        ):
            with self.assertRaises(UnknownWriteResultError):
                with connect({"type": "sqlite", "path": ":memory:"}, None):
                    raise UnknownWriteResultError("result unknown")

    def test_cli_sqlite_direct_query_ddl_backup(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / "config"
            database = root / "sample.sqlite"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    "CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT NOT NULL);"
                    "CREATE INDEX users_name_idx ON users(name);"
                    "INSERT INTO users(name) VALUES ('Ada'), ('Linus');"
                )
            env = {"DBQ_MASTER_PASSWORD": "correct horse battery staple"}
            with mock.patch.dict(os.environ, env, clear=False):
                self.assertEqual(main(["--config-dir", str(config), "--non-interactive", "init", "--no-remember"]), 0)
                self.assertEqual(
                    main(["conn", "add", "local", "--type", "sqlite", "--path", str(database), "--config-dir", str(config), "--non-interactive"]),
                    0,
                )
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = main(["local", "SELECT id, name FROM users ORDER BY id", "--config-dir", str(config), "--json"])
                self.assertEqual(code, 0)
                payload = json.loads(output.getvalue())
                self.assertEqual(payload["row_count"], 2)
                self.assertEqual(payload["rows"][0], [1, "Ada"])
                output = io.StringIO()
                with mock.patch("sys.stdin", io.StringIO("SELECT COUNT(*) AS total FROM users")), contextlib.redirect_stdout(output):
                    self.assertEqual(main(["local", "-i", "-", "--config-dir", str(config), "--json"]), 0)
                self.assertEqual(json.loads(output.getvalue())["rows"], [[2]])
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["ddl", "local", "users", "--config-dir", str(config)]), 0)
                self.assertIn("CREATE TABLE users", output.getvalue())
                self.assertIn("CREATE INDEX users_name_idx", output.getvalue())
                exported = root / "users.csv"
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["local", "SELECT id, name FROM users ORDER BY id", "-o", str(exported), "--config-dir", str(config)]), 0)
                self.assertEqual(exported.read_text(encoding="utf-8").splitlines()[1], "1,Ada")
                if os.name != "nt":
                    self.assertEqual(exported.stat().st_mode & 0o077, 0)
                archive = root / "settings.dbq-backup"
                self.assertEqual(main(["backup", "create", "-o", str(archive), "--config-dir", str(config)]), 0)
                if os.name != "nt":
                    self.assertEqual(archive.stat().st_mode & 0o077, 0)
                restored = root / "restored"
                self.assertEqual(main(["backup", "restore", str(archive), "--to-dir", str(restored), "--config-dir", str(config)]), 0)
                self.assertTrue((restored / "vault.dbq").exists())
                self.assertEqual(ConfigStore(restored).get_connection("default", "dev", "local")["type"], "sqlite")
                portable = root / "portable.dbq-backup"
                with mock.patch("sys.stdin", io.StringIO("independent backup password\n")):
                    self.assertEqual(
                        main(["backup", "create", "-o", str(portable), "--rekey", "--backup-password-stdin", "--non-interactive", "--config-dir", str(config)]),
                        0,
                    )
                with mock.patch("sys.stdin", io.StringIO("independent backup password\n")):
                    self.assertEqual(
                        main(["backup", "verify", str(portable), "--password-stdin", "--non-interactive", "--config-dir", str(config)]),
                        0,
                    )

    def test_network_password_never_enters_plain_config(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            store = ConfigStore(root)
            store.initialize()
            Vault(root).initialize("correct horse battery staple", remember=False)
            with mock.patch.dict(os.environ, {"DBQ_MASTER_PASSWORD": "correct horse battery staple"}, clear=False), mock.patch(
                "sys.stdin", io.StringIO("database-password\n")
            ):
                code = main([
                    "conn", "add", "finance", "--type", "mysql", "--host", "db.example",
                    "--database", "finance", "--user", "reader", "--password-stdin",
                    "--non-interactive", "--config-dir", str(root),
                ])
            self.assertEqual(code, 0)
            self.assertNotIn("database-password", (root / "config.json").read_text(encoding="utf-8"))
            self.assertNotIn("database-password", (root / "vault.dbq").read_text(encoding="utf-8"))

    def test_duplicate_connection_rolls_back_vault_and_sqlite_edit_removes_secret(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            database = root / "sample.sqlite"
            sqlite3.connect(database).close()
            store = ConfigStore(root)
            store.initialize()
            vault = Vault(root)
            vault.initialize("correct horse battery staple", remember=False)
            unlocked = vault.unlock("correct horse battery staple")
            unlocked.set("default/dev/app", "old-secret")
            store.put_connection(
                "default", "dev", "app",
                {"type": "mysql", "host": "db.example", "database": "app", "user": "reader", "tls_mode": "verify-full", "readonly": True, "allow_ddl": False, "credential_ref": "default/dev/app"},
            )
            environment = {"DBQ_MASTER_PASSWORD": "correct horse battery staple"}
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch(
                "sys.stdin", io.StringIO("new-secret\n")
            ):
                code = main([
                    "conn", "add", "app", "--type", "mysql", "--host", "other.example",
                    "--database", "app", "--user", "reader", "--password-stdin",
                    "--non-interactive", "--config-dir", str(root),
                ])
            self.assertEqual(code, ConfigError.exit_code)
            self.assertEqual(vault.unlock("correct horse battery staple").get("default/dev/app"), "old-secret")
            with mock.patch.dict(os.environ, environment, clear=False):
                self.assertEqual(
                    main(["conn", "edit", "app", "--type", "sqlite", "--path", str(database), "--non-interactive", "--config-dir", str(root)]),
                    0,
                )
            self.assertNotIn("credential_ref", store.get_connection("default", "dev", "app"))
            self.assertIsNone(vault.unlock("correct horse battery staple").get("default/dev/app"))

    def test_legacy_migration_preview_is_redacted_and_permissions_tighten(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            old = root / "legacy"
            old.mkdir()
            (old / "connections.prod.yaml").write_text(
                "connections:\n"
                "  billing:\n"
                "    type: mysql\n"
                "    host: db.example\n"
                "    database: billing\n"
                "    user: admin\n"
                "    password: legacy-secret\n"
                "    url: mysql://admin:also-secret@db.example/billing\n"
                "    tls_mode: disable\n"
                "    readonly: false\n"
                "    allow_ddl: true\n",
                encoding="utf-8",
            )
            shown = preview(old)
            self.assertNotIn("legacy-secret", json.dumps(shown))
            target = root / "new"
            store = ConfigStore(target)
            store.initialize()
            vault = Vault(target)
            vault.initialize("correct horse battery staple", remember=False)
            unlocked = vault.unlock("correct horse battery staple")
            result = migrate(old, store, unlocked)
            info = store.get_connection("default", "prod", "billing")
            self.assertTrue(info["readonly"])
            self.assertFalse(info["allow_ddl"])
            self.assertEqual(info["tls_mode"], "verify-full")
            self.assertNotIn("url", info)
            self.assertEqual(result[0]["credential"], "migrated")
            self.assertNotIn("legacy-secret", (target / "config.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
