import base64
import json
import logging
import time as _time
import urllib.parse
import uuid
from pathlib import Path

import requests

from .auth import APP_VERSION, AlexaAuth
from .exception import AlexaAPIError

logger = logging.getLogger(__name__)

USER_AGENT = (
    f"PitanguiBridge/{APP_VERSION}-[PLATFORM=Android][MANUFACTURER=Google][RELEASE=11]"
    "[BRAND=google][SDK=30][MODEL=sdk_gphone_x86_64]"
)
ALERTS_BASE = "https://api.amazonalexa.com/v1/alerts/alarms"
PROFILE_URL = "https://api.amazon.com/user/profile"
ALEXA_APP_HEADER = base64.b64encode(json.dumps(
    {"appId": "amzn1.application.d8248c711e884e7baf99e6b37c7c28b0", "version": "1.0", "appVersion": "2022.10.10"},
    separators=(",", ":"),
).encode()).decode()

# Web API host per Amazon domain. Only na-api-alexa.amazon.com has been verified against real traffic.
WEB_HOSTS = {
    "amazon.com": "na-api-alexa.amazon.com",
    "amazon.ca": "na-api-alexa.amazon.com",
    "amazon.com.mx": "na-api-alexa.amazon.com",
    "amazon.com.br": "na-api-alexa.amazon.com",
    "amazon.co.uk": "eu-api-alexa.amazon.co.uk",
    "amazon.de": "eu-api-alexa.amazon.co.uk",
    "amazon.fr": "eu-api-alexa.amazon.co.uk",
    "amazon.it": "eu-api-alexa.amazon.co.uk",
    "amazon.es": "eu-api-alexa.amazon.co.uk",
    "amazon.in": "eu-api-alexa.amazon.co.uk",
    "amazon.co.jp": "fe-api-alexa.amazon.co.jp",
    "amazon.com.au": "fe-api-alexa.amazon.co.jp",
}

# Paths that set the `csrf` cookie when fetched with valid session cookies; tried in order.
_CSRF_BOOTSTRAP_PATHS = ("/api/language", "/api/devices-v2/device?cached=false", "/spa/index.html")

TEXT_COMMAND_SKILL = "amzn1.ask.1p.tellalexa"


class _AmazonSession(requests.Session):

    def request(self, method, url, **kwargs) -> requests.Response:
        kwargs.setdefault("headers", {})["x-amzn-RequestId"] = str(uuid.uuid4())
        kwargs.setdefault("timeout", 30)
        return super().request(method, url, **kwargs)


def _json(response: requests.Response) -> "dict | list | None":
    if not response.text:
        return None
    try:
        return response.json()
    except requests.exceptions.JSONDecodeError:
        if "api-services-support@amazon.com" in response.text:
            logger.warning("Alexa returned an automated-access block page. Re-run the cookie login on this machine.")
        else:
            logger.warning("Invalid JSON from Alexa (HTTP %d): %s", response.status_code, response.text[:200])
        return None


RATE_LIMIT_DELAYS = (2.0, 4.0, 8.0)   # seconds to wait before each retry after HTTP 429


def _retry_after(response) -> "float | None":
    """The server's Retry-After in seconds (capped at 30), if it sent a number."""
    try:
        return min(float((getattr(response, "headers", None) or {}).get("Retry-After")), 30.0)
    except (TypeError, ValueError):
        return None


def _log_exchange(method: str, path: str, kwargs: dict, response) -> None:
    """With -vv: request/response bodies (never headers, which hold tokens and cookies)."""
    if logger.isEnabledFor(logging.DEBUG):
        sent = json.dumps(kwargs["json"]) if "json" in kwargs else ""
        logger.debug("%s %s -> HTTP %s\n  sent: %.4000s\n  got:  %.4000s",
                     method, path, response.status_code, sent, getattr(response, "text", ""))


class AlexaAPI:
    """Raw access to Alexa alarms, reminders, timers, devices, sounds and text commands."""

    def __init__(
        self,
        amazon_domain: str = "amazon.com",
        service_auth_path: "Path | str | None" = None,
        runtime_creds_path: "Path | str | None" = None,
        alexa_host: "str | None" = None,
        person_id: "str | None" = None,
        locale: str = "en-US",
    ) -> None:
        self._host = alexa_host or WEB_HOSTS.get(amazon_domain, f"alexa.{amazon_domain}")
        self._base = f"https://{self._host}"
        self.locale = locale
        self._session = _AmazonSession()
        self._session.headers.update({
            "Accept": "application/json; charset=utf-8",
            "Accept-Language": locale,
            "User-Agent": USER_AGENT,
        })
        self._auth = AlexaAuth(self._session, amazon_domain, service_auth_path, runtime_creds_path)
        self._csrf: str | None = None
        self._person_id = person_id

    def _cookie(self, name: str) -> "str | None":
        return next((c.value for c in self._session.cookies if c.name == name), None)

    def _ensure_csrf(self) -> "str | None":
        if self._csrf:
            return self._csrf
        token = self._cookie("csrf")
        for path in _CSRF_BOOTSTRAP_PATHS:
            if token:
                break
            try:
                self._session.get(self._base + path)
            except requests.RequestException as e:
                logger.debug("csrf bootstrap %s failed: %s", path, e)
            token = self._cookie("csrf")
        if not token:
            logger.warning("Could not obtain a csrf token from %s; changes will likely fail", self._host)
        self._csrf = token
        return token

    def _send(self, method: str, url: str, label: str, headers: dict, kwargs: dict) -> requests.Response:
        """Send one request. On HTTP 429 (rate limited, so nothing was done) wait and retry, a few times."""
        for delay in (*RATE_LIMIT_DELAYS, None):
            response = self._session.request(method, url, headers=headers, **kwargs)
            _log_exchange(method, label, kwargs, response)
            if response.status_code != 429 or delay is None:
                return response
            wait = _retry_after(response) or delay
            logger.warning("%s %s: Alexa is rate limiting requests (HTTP 429); retrying in %g s", method, label, wait)
            _time.sleep(wait)
        raise AssertionError("unreachable")

    def _web(self, method: str, path: str, *, _retried: bool = False, **kwargs) -> requests.Response:
        self._auth.check_expiry()
        headers = kwargs.pop("headers", {})
        csrf = self._ensure_csrf()
        if csrf:
            headers["csrf"] = csrf
        if "json" in kwargs:
            headers["Content-Type"] = "application/json; charset=utf-8"
        response = self._send(method, self._base + path, path, headers, kwargs)
        if response.status_code in (401, 403) and not _retried:
            logger.info("%s %s returned HTTP %d; refreshing cookies and retrying", method, path, response.status_code)
            if self._auth.refresh():
                self._csrf = None
                return self._web(method, path, _retried=True, headers=headers, **kwargs)
        if not response.ok:
            raise AlexaAPIError(method, path, response.status_code, response.text)
        return response

    def _alerts(self, method: str, path: str = "", *, _retried: bool = False, **kwargs) -> requests.Response:
        headers = {
            "authorization": self._auth.access_token(force=_retried),
            "x-amzn-alexa-app": ALEXA_APP_HEADER,
            "x-amzn-aaas-skip-region-check": "1p-trusted",
            "cache-control": "no-cache",
        }
        if "json" in kwargs:
            headers["Content-Type"] = "application/json; charset=utf-8"
        url = path if path.startswith("https://") else ALERTS_BASE + path
        response = self._send(method, url, url, headers, kwargs)
        if response.status_code in (401, 403) and not _retried:
            logger.info("%s %s returned HTTP %d; minting a new access token and retrying", method, url, response.status_code)
            return self._alerts(method, path, _retried=True, **kwargs)
        if not response.ok:
            raise AlexaAPIError(method, url, response.status_code, response.text)
        return response

    @property
    def personId(self) -> str:
        """The account's `amzn1.account.…` id, needed by the reminders/timers endpoints."""
        if self._person_id:
            return self._person_id
        pid = self._auth.config_value("person_id") or self._lookup_person_id()
        if not pid:
            raise LookupError(
                "Could not determine your Amazon account id. Add it to service_auth.json as "
                "alexa.person_id (it looks like amzn1.account.XXXX), or pass --person-id."
            )
        self._person_id = pid
        return pid

    def _lookup_person_id(self) -> "str | None":
        try:
            r = self._session.get(PROFILE_URL, headers={"Authorization": f"Bearer {self._auth.access_token()}"})
            if r.ok and str(r.json().get("user_id", "")).startswith("amzn1.account."):
                pid = r.json()["user_id"]
                self._auth.cache_value("person_id", pid)
                return pid
            logger.debug("profile lookup returned HTTP %d", r.status_code)
        except Exception as e:  # noqa: BLE001 - best effort
            logger.debug("profile lookup failed: %s", e)
        try:
            data = _json(self._web("GET", "/api/notifications?notificationType=REMINDER&clientType=VOX")) or {}
            for n in data.get("notifications", []):
                pid = (n.get("personProfile") or {}).get("personId")
                if pid:
                    self._auth.cache_value("person_id", pid)
                    return pid
        except AlexaAPIError as e:
            logger.debug("reminder list without personId failed: %s", e)
        return None

    def getDevices(self) -> list[dict]:
        data = _json(self._web("GET", "/api/devices-v2/device?cached=false")) or {}
        return data.get("devices", [])

    def getSounds(self, serial: str, device_type: str, alert_type: str = "Alarm") -> list[dict]:
        q = urllib.parse.urlencode({"deviceType": device_type, "deviceSerialNumber": serial})
        data = _json(self._web("GET", f"/api/notification/sounds?{q}&softwareVersion=%27%27&alertType={alert_type}")) or {}
        return data.get("notificationSounds", [])

    def getDefaultSound(self, serial: str, device_type: str, notification_type: str = "Alarm") -> "dict | None":
        q = urllib.parse.urlencode({"deviceType": device_type, "deviceSerialNumber": serial})
        return _json(self._web(
            "GET", f"/api/notification/default-sound?{q}&notificationType={notification_type}"))

    def getAlarms(self) -> list[dict]:
        alarms, url, seen = [], "?maxResults=200", set()
        while url and url not in seen:
            seen.add(url)
            data = _json(self._alerts("GET", url)) or {}
            alarms += data.get("alarms", [])
            nxt = (data.get("links") or {}).get("next")
            if not nxt:
                break
            url = nxt if nxt.startswith(("https://", "?")) else (
                "https://api.amazonalexa.com" + nxt if nxt.startswith("/") else None)
        return alarms

    def createAlarm(self, body: dict) -> "dict | None":
        return _json(self._alerts("POST", json=body))

    def updateAlarm(self, token: str, body: dict) -> "dict | None":
        return _json(self._alerts("PUT", f"/{token}", json=body))

    def activateAlarm(self, token: str, body: dict) -> "dict | None":
        return _json(self._alerts("PUT", f"/{token}/activate", json=body))

    def cancelAlarm(self, token: str) -> "dict | None":
        return _json(self._alerts("PUT", f"/{token}/cancel"))

    def deleteAlarm(self, token: str) -> None:
        self._alerts("DELETE", f"/{token}")

    def getNotifications(self, notification_type: str) -> list[dict]:
        """REMINDER or TIMER notifications. Reminder results also contain to-do Tasks; callers filter them."""
        q = {"personId": self.personId, "notificationType": notification_type}
        if notification_type == "REMINDER":
            q["clientType"] = "VOX"
        data = _json(self._web("GET", "/api/notifications?" + urllib.parse.urlencode(q))) or {}
        return data.get("notifications", [])

    def createReminder(self, body: dict) -> "dict | None":
        return _json(self._web("PUT", "/api/notifications/null", json=body))

    def updateReminder(self, body: dict) -> "dict | None":
        return _json(self._web("PUT", f"/api/notifications/{urllib.parse.quote(body['id'])}", json=body))

    def deleteReminder(self, notification_id: str) -> None:
        q = urllib.parse.urlencode({"personId": self.personId, "clientType": "VOX"})
        self._web("DELETE", f"/api/notifications/{urllib.parse.quote(notification_id)}?{q}")

    def updateTimer(self, body: dict) -> "dict | None":
        """Pause/resume: PUT the whole timer with a new status (no query string)."""
        return _json(self._web("PUT", f"/api/notifications/{urllib.parse.quote(body['id'])}", json=body))

    def deleteTimer(self, notification_id: str) -> None:
        q = urllib.parse.urlencode({"personId": self.personId})
        self._web("DELETE", f"/api/notifications/{urllib.parse.quote(notification_id)}?{q}")

    def _runNode(self, node: dict) -> None:
        """Run a one-step routine right away."""
        sequence = {
            "@type": "com.amazon.alexa.behaviors.model.Sequence",
            "startNode": {"@type": "com.amazon.alexa.behaviors.model.OpaquePayloadOperationNode", **node},
        }
        body = {"behaviorId": "PREVIEW", "sequenceJson": json.dumps(sequence), "status": "ENABLED"}
        self._web("POST", "/api/behaviors/preview", json=body)

    def runTextCommand(self, serial: str, device_type: str, customer_id: str, text: str) -> None:
        """Have a device act on `text` as if it had been spoken to it (timers, stop, snooze)."""
        self._runNode({
            "type": "Alexa.TextCommand",
            "skillId": TEXT_COMMAND_SKILL,
            "operationPayload": {
                "deviceType": device_type,
                "deviceSerialNumber": serial,
                "locale": self.locale,
                "customerId": customer_id,
                "text": text,
            },
        })
