class DbqError(Exception):
    """Base error with a stable process exit code."""

    exit_code = 1


class UsageError(DbqError):
    exit_code = 2


class ConfigError(DbqError):
    exit_code = 3


class PolicyError(DbqError):
    exit_code = 4


class ConnectionError(DbqError):
    exit_code = 5


class QueryError(DbqError):
    exit_code = 6


class TimeoutError(DbqError):
    exit_code = 7


class UnknownWriteResultError(DbqError):
    exit_code = 8


class BackupError(DbqError):
    exit_code = 9

