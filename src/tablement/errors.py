"""Exception hierarchy for Tablement."""


class TablementError(Exception):
    """Base exception."""


class AuthError(TablementError):
    """Authentication failed."""


class NoSlotsError(TablementError):
    """No slots available."""


class BookingError(TablementError):
    """Booking step failed."""


class ExhaustedRetriesError(TablementError):
    """Retry window exhausted without success."""


class VenueLookupError(TablementError):
    """Could not resolve venue."""


class ConfigError(TablementError):
    """Invalid configuration."""


# --- OpenTable-specific errors ---


class OpenTableError(TablementError):
    """Base exception for OpenTable operations."""


class OTAuthError(OpenTableError):
    """OpenTable authentication / token validation failed."""


class OTLockError(OpenTableError):
    """Failed to lock (temporarily hold) an OpenTable slot."""


class OTRateLimitError(OpenTableError):
    """OpenTable rate limit hit — back off before retrying."""
