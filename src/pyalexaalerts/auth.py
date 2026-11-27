import datetime
import json
import logging
import sys
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

from .exception import AlexaSessionExpiredException

logger = logging.getLogger(__name__)

_PYTHON_CMD = Path(sys.executable).stem

TOKEN_EXCHANGE_URL = "https://api.amazon.com/ap/exchangetoken/cookies"
ACCESS_TOKEN_URL = "https://api.amazon.com/auth/token"
APP_VERSION = "2.2.704787.0"

SUPPORTED_DOMAINS = (
    "amazon.com", "amazon.co.uk", "amazon.de", "amazon.fr", "amazon.it", "amazon.es",
    "amazon.co.jp", "amazon.ca", "amazon.com.au", "amazon.in", "amazon.com.br", "amazon.com.mx",
)


def default_service_auth_path() -> Path:
    return Path.cwd() / "config" / "service_auth.json"


def exchange_refresh_token(refresh_token: str, amazon_domain: str = "amazon.com") -> dict:
    """Exchange an Alexa refresh token for session cookies.

    Returns:
        Dict of cookie name -> raw cookie dict (keys Name, Value, Expires, ...).
    Raises:
        requests.RequestException / KeyError on failure.
    """
    response = requests.post(
        TOKEN_EXCHANGE_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "x-amzn-identity-auth-domain": "api.amazon.com",
        },
        data={
            "requested_token_type": "auth_cookies",
            "app_name": "Amazon Alexa",
            "domain": f"www.{amazon_domain}",
            "source_token_type": "refresh_token",
            "source_token": refresh_token,
        },
        timeout=30,
    )
    response.raise_for_status()
    raw = response.json()["response"]["tokens"]["cookies"][f".{amazon_domain}"]
    return {c["Name"]: c for c in raw}


def mint_access_token(refresh_token: str) -> "tuple[str, datetime.datetime]":
    """Exchange an Alexa refresh token for a short-lived `Atna|...` access token.

    The new alarms API (api.amazonalexa.com) authenticates with this token instead of cookies.

    Returns:
        (token, expires_at_utc)
    """
    response = requests.post(
        ACCESS_TOKEN_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "x-amzn-identity-auth-domain": "api.amazon.com",
        },
        data={
            "app_name": "Amazon Alexa",
            "app_version": APP_VERSION,
            "source_token": refresh_token,
            "source_token_type": "refresh_token",
            "requested_token_type": "access_token",
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=int(data.get("expires_in", 3600)))
    return data["access_token"], expires


class AlexaAuth:
    """Loads, validates and refreshes Alexa session cookies, and applies them to a requests.Session."""

    def __init__(
        self,
        session: requests.Session,
        amazon_domain: str = "amazon.com",
        service_auth_path: "Path | str | None" = None,
        runtime_creds_path: "Path | str | None" = None,
    ) -> None:
        if amazon_domain not in SUPPORTED_DOMAINS:
            raise ValueError(f"Unsupported amazon_domain '{amazon_domain}'. Supported: {sorted(SUPPORTED_DOMAINS)}")
        self._session = session
        self._amazon_domain = amazon_domain
        self._service_auth_path = Path(service_auth_path) if service_auth_path else default_service_auth_path()
        self._runtime_creds_path = (
            Path(runtime_creds_path) if runtime_creds_path
            else self._service_auth_path.parent / "runtime_credentials.json"
        )
        self._session_expiry: datetime.datetime | None = None
        self._last_expiry_warning: datetime.datetime | None = None
        self.load()

    @property
    def session_expiry(self) -> "datetime.datetime | None":
        return self._session_expiry

    @staticmethod
    def _earliest_expiry(cookies: dict) -> "datetime.datetime | None":
        expiries = [parsedate_to_datetime(c["Expires"]) for c in cookies.values() if c.get("Expires")]
        return min(expiries) if expiries else None

    def load(self) -> None:
        """Load cached cookies, bootstrapping via token exchange if the cache is missing."""
        if not self._runtime_creds_path.exists():
            logger.info("%s not found — bootstrapping via token exchange", self._runtime_creds_path.name)
            if not self.refresh():
                raise FileNotFoundError(
                    f"Alexa credentials not found at {self._runtime_creds_path} and auto-refresh failed. "
                    f"Ensure {self._service_auth_path} has a valid alexa.refresh_token."
                )
            return
        with open(self._runtime_creds_path, "r") as f:
            cookies = json.load(f)["alexa"]["cookies"]
        self._apply(cookies)
        self.check_expiry()

    def _apply(self, cookies: dict) -> None:
        self._session_expiry = self._earliest_expiry(cookies)
        for name, cookie in cookies.items():
            self._session.cookies.set(name, cookie["Value"], domain=f".{self._amazon_domain}", path="/")

    def _service_auth(self) -> dict:
        try:
            with open(self._service_auth_path, "r") as f:
                return json.load(f).get("alexa", {})
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _refresh_token(self) -> "str | None":
        token = self._service_auth().get("refresh_token")
        if not token:
            logger.warning("No alexa.refresh_token in %s", self._service_auth_path)
        return token or None

    def _runtime(self) -> dict:
        try:
            with open(self._runtime_creds_path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_runtime(self, key: str, value) -> None:
        try:
            runtime = self._runtime()
            runtime.setdefault("alexa", {})[key] = value
            self._runtime_creds_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._runtime_creds_path, "w") as f:
                json.dump(runtime, f, indent=4)
        except Exception as e:
            logger.warning("Could not write %s to %s: %s", key, self._runtime_creds_path, e)

    def config_value(self, key: str) -> "str | None":
        """A value from service_auth.json's `alexa` section, else the runtime cache (e.g. person_id)."""
        return self._service_auth().get(key) or self._runtime().get("alexa", {}).get(key)

    def cache_value(self, key: str, value: str) -> None:
        self._save_runtime(key, value)

    def refresh(self) -> bool:
        """Exchange the refresh token for new cookies, cache them, and apply them.

        Returns:
            True on success, False on any failure (logged).
        """
        refresh_token = self._refresh_token()
        if not refresh_token:
            return False
        try:
            cookies = exchange_refresh_token(refresh_token, self._amazon_domain)
        except Exception as e:
            logger.warning("Auto-refresh failed — token exchange error: %s", e)
            return False
        self._save_runtime("cookies", cookies)  # cookies stay usable in-memory even if this fails
        self._apply(cookies)
        return True

    def access_token(self, force: bool = False) -> str:
        """A valid `Atna|...` access token, minted from the refresh token and cached for its lifetime."""
        now = datetime.datetime.now(datetime.timezone.utc)
        if not force:
            cached = self._runtime().get("alexa", {}).get("access_token") or {}
            try:
                expires = datetime.datetime.fromisoformat(cached["expires_at"])
                if expires - now > datetime.timedelta(minutes=2):
                    return cached["value"]
            except (KeyError, TypeError, ValueError):
                pass
        refresh_token = self._refresh_token()
        if not refresh_token:
            raise FileNotFoundError(f"An alexa.refresh_token is required in {self._service_auth_path}")
        token, expires = mint_access_token(refresh_token)
        self._save_runtime("access_token", {"value": token, "expires_at": expires.isoformat()})
        return token

    def check_expiry(self) -> None:
        """Warn when the session is close to expiry; auto-refresh when expired.

        Raises:
            AlexaSessionExpiredException: if expired and the refresh fails.
        """
        if self._session_expiry is None:
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        if self._session_expiry > now:
            days_left = (self._session_expiry - now).days
            if days_left <= 30 and (
                self._last_expiry_warning is None
                or (now - self._last_expiry_warning).total_seconds() >= 3600
            ):
                logger.warning(
                    "Alexa session expires in %s day(s) on %s — it will auto-refresh, or re-run %s -m pyalexaalerts.get_alexa_cookies",
                    days_left, self._session_expiry.astimezone().strftime("%Y-%m-%d %H:%M %Z"), _PYTHON_CMD,
                )
                self._last_expiry_warning = now
            return

        logger.warning("Alexa session expired — attempting automatic token refresh")
        if self.refresh():
            logger.info("Alexa session refreshed via token exchange")
            return
        raise AlexaSessionExpiredException(self._session_expiry)
