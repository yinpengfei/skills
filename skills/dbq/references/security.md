# dbq credential and recovery rules

- Store connection metadata in `config.json` and encrypted credentials in `vault.dbq`.
- Encrypt credentials with a random AES-256-GCM data key. Wrap that key with an Argon2id-derived unlock key.
- Store only the derived unlock key in the operating system credential store. Windows Credential Manager, macOS Keychain, and Linux Secret Service are accessed through Python keyring when available.
- For headless automation, inject either `DBQ_MASTER_PASSWORD` or `DBQ_MASTER_PASSWORD_FILE`. Reject simultaneous sources and never fall back after an explicitly supplied value fails.
- Use `dbq vault unlock --remember` after copying a configuration directory to a new machine. The original master password remains the portable recovery mechanism.
- `dbq init --master-mode generate` prints a recovery secret and is only for a person running it in their own terminal. An Agent must not invoke this mode or capture the generated value.
- Stop dbq writes before copying the whole configuration directory. Prefer `dbq backup create` for a verified snapshot. Use `--rekey` to protect the backup with an independent password.
- Never inspect the vault, password files, legacy `.env`, or database URLs to recover a credential. Use dbq commands and report only `set`, `missing`, or `not-required` credential status.
- Do not treat tool policy as business authorization. A production write must carry the exact `--confirm-target` for that invocation.
