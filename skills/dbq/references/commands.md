# dbq command reference

## Connection and environment

```text
dbq init [--master-mode prompt|generate]
dbq env list
dbq env add NAME [--production]
dbq conn add NAME --type mysql|mariadb|postgresql --host HOST --database DB --user USER
dbq conn add NAME --type sqlite --path FILE
dbq conn edit NAME [connection options]
dbq conn ls [--all]
dbq conn show NAME
dbq conn passwd NAME [--password-stdin]
dbq conn test NAME
dbq conn rm NAME
dbq migrate legacy --source PATH --dry-run
dbq migrate legacy --source PATH
```

`dbq init` creates `dev`, `test`, and `prod`; `prod` is marked as production and always requires explicit `-e prod`.

`conn` has the long alias `connection`; `ls` and `list` are interchangeable under `conn`. Interactive password entry is hidden. Automation must use `--password-stdin`; there is no plaintext `--password` option.

Preview legacy migration first. The source YAML, `.env`, and legacy keychain entries remain untouched; imported write permissions are reset to read-only.

## Query and metadata

```text
dbq ALIAS 'SELECT ...'
dbq query ALIAS 'SELECT ...'
dbq q ALIAS -i query.sql --params-file params.json
cat query.sql | dbq q ALIAS -i -
dbq count ALIAS 'SELECT ...'
dbq explain ALIAS 'SELECT ...'
dbq tables ALIAS [--search TEXT] [--pattern GLOB] [--schema NAME]
dbq desc ALIAS TABLE...
dbq ddl ALIAS TABLE...
dbq inspect ALIAS TABLE... [--ddl]
```

The first form treats the first word as a configured connection alias unless it is a reserved command. `query` is the unambiguous fallback. `q`, `ex`, `desc`, `ls`, and `info` are published short aliases.

`-i -` reads SQL from stdin. SQL input is limited to 8 MiB; database passwords use a separate command and never share stdin with a query.

Global options may appear before or after a leaf command: `-p/--project`, `-e/--env`, `-t/--timeout`, `-f/--format`, `--json`, `--config-dir`, and `--non-interactive`. Conflicting duplicate values fail.

Timeout accepts seconds, `10m`, `1h`, or `0`. Connection timeout defaults to 10 seconds. Query timeout has no dbq default; database and proxy limits still apply.

## Controlled writes

```text
dbq exec ALIAS --sql-file change.sql --dry-run -e test
dbq exec ALIAS --sql-file change.sql -e prod \
  --confirm-target default/prod/ALIAS
```

DML requires `readonly: false`; execution of DDL requires `allow_ddl: true`. Viewing DDL never requires write permission. UPDATE and DELETE require a real top-level condition. `WHERE 1=1` is rejected.

## Backup and recovery

```text
dbq backup create -o settings.dbq-backup
dbq backup create -o portable.dbq-backup --rekey
dbq backup verify settings.dbq-backup
dbq backup restore settings.dbq-backup --to-dir PATH
```

For non-interactive rekeying, pipe the new independent password to `--backup-password-stdin`. For non-interactive verify or restore, pipe the backup's password to `--password-stdin` or use one of the supported master-password environment sources.
