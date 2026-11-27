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


class PortabilityError(BunnyError):
    """Base error for export and import; the profile was not changed."""

    def __init__(self, message: str, problems: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.problems = problems


class ExportError(PortabilityError):
    """The export file could not be written safely."""


class ImportValidationError(PortabilityError):
    """The import file is malformed or violates the documented format."""


class UnsupportedImportError(PortabilityError):
    """The import file declares a format or version this build does not support."""


class ImportConflictError(PortabilityError):
    """The import conflicts with local data and was refused in full."""


class GatewayError(BunnyError):
    """Base error for provider-neutral gateway, pricing, and ledger failures."""

    def __init__(self, message: str, *, refusal_code: str | None = None) -> None:
        super().__init__(message)
        self.refusal_code = refusal_code


class PricingError(GatewayError):
    """Missing, incomplete, expired, or mismatched pricing metadata."""


class BudgetError(GatewayError):
    """Pre-flight budget policy refused the request before transport dispatch."""


class DispatchBlockedError(GatewayError):
    """Profile dispatch is blocked pending operator reconciliation or review."""


class NotDispatchedError(GatewayError):
    """Transport confirmed the request was not sent to any upstream provider."""


class TransportUncertainError(GatewayError):
    """Transport outcome is uncertain; reservation must be held until reconciled."""
