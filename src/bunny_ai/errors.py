"""Typed errors for Bunny.ai configuration and local storage."""


class BunnyError(Exception):
    """Base error for Bunny.ai runtime failures."""


class ConfigError(BunnyError):
    """Invalid, incomplete, or unsafe configuration."""


class StorageError(BunnyError):
    """Base error for local SQLite or filesystem state failures."""


class CorruptDatabaseError(StorageError):
    """Database file is unreadable, truncated, or failed integrity verification."""


class NewerSchemaError(StorageError):
    """Database schema version is newer than this version of Bunny.ai supports."""


class IncompatibleSchemaError(StorageError):
    """Pre-existing unversioned or foreign database schema was found."""


class MissingIdentityError(StorageError):
    """Initialized database is missing its required identity record."""


class MigrationError(StorageError):
    """Preflight backup or transactional schema migration failed."""


class RecoveryRequiredError(StorageError):
    """Database has an unrecovered rollback journal that read-only inspection cannot replay."""


class DatabaseLockedError(StorageError):
    """Database or storage lock is held by another active process."""
