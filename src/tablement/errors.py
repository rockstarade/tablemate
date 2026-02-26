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
