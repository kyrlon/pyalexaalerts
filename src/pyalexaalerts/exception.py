import datetime
import sys
from pathlib import Path

_PYTHON_CMD = Path(sys.executable).stem


class AlexaSessionExpiredException(Exception):
    """Raised when the Alexa session cookies have expired and could not be refreshed."""

    def __init__(self, expired_at: datetime.datetime) -> None:
        super().__init__(
            f"Alexa session expired on {expired_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
            f" ({expired_at.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}) — "
            f"re-run {_PYTHON_CMD} -m pyalexaalerts.get_alexa_cookies to refresh"
        )
        self.expired_at = expired_at


class AlexaAPIError(Exception):
    """Raised when the Alexa API returns a non-2xx response."""

    def __init__(self, method: str, path: str, status_code: int, body: str) -> None:
        super().__init__(f"{method} {path} failed with HTTP {status_code}: {body[:300]}")
        self.method = method
        self.path = path
        self.status_code = status_code
        self.body = body


class DeviceNotFoundException(LookupError):
    """Raised when a device name/serial matches no device, or more than one."""


class NotificationNotFoundException(LookupError):
    """Raised when an alarm/reminder reference matches no notification, or more than one."""
