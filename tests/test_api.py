import base64
import datetime
import json

import pytest

from pyalexaalerts.api import AlexaAPI

FAR = "Wed, 01 Jan 2099 00:00:00 GMT"


class _Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.text = json.dumps(body) if body is not None else ""
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


@pytest.fixture
def api(tmp_path, monkeypatch):
    (tmp_path / "service_auth.json").write_text(json.dumps({"alexa": {"refresh_token": "RT", "person_id": "amzn1.account.X"}}))
    (tmp_path / "runtime_credentials.json").write_text(json.dumps({"alexa": {
        "cookies": {
            "session-token": {"Name": "session-token", "Value": "s", "Expires": FAR},
            "csrf": {"Name": "csrf", "Value": "tok123", "Expires": FAR},
        },
        "access_token": {"value": "Atna|cached",
                         "expires_at": (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).isoformat()},
    }}))
    a = AlexaAPI(service_auth_path=tmp_path / "service_auth.json")
    a.seen = []
    a.responses = []

    def fake_request(method, url, **kw):
        a.seen.append((method, url, kw))
        return a.responses.pop(0) if a.responses else _Resp(200, {})

    monkeypatch.setattr(a._session, "request", fake_request)
    return a


def test_alarm_requests_use_atna_token(api):
    api.responses = [_Resp(200, {"alarms": [{"alarmToken": "t1"}], "links": {}})]
    assert api.getAlarms() == [{"alarmToken": "t1"}]
    method, url, kw = api.seen[-1]
    assert (method, url) == ("GET", "https://api.amazonalexa.com/v1/alerts/alarms?maxResults=200")
    h = kw["headers"]
    assert h["authorization"] == "Atna|cached"  # no "Bearer" prefix
    assert h["x-amzn-aaas-skip-region-check"] == "1p-trusted"
    assert json.loads(base64.b64decode(h["x-amzn-alexa-app"]))["appId"] == "amzn1.application.d8248c711e884e7baf99e6b37c7c28b0"
    assert "csrf" not in h

    api.activateAlarm("t1", {"x": 1})
    api.cancelAlarm("t1")
    api.deleteAlarm("t1")
    assert [(m, u.rsplit("alarms", 1)[1]) for m, u, _ in api.seen[-3:]] == [
        ("PUT", "/t1/activate"), ("PUT", "/t1/cancel"), ("DELETE", "/t1")]


def test_429_waits_and_retries(api, monkeypatch):
    slept = []
    monkeypatch.setattr("pyalexaalerts.api._time.sleep", slept.append)
    api.responses = [_Resp(429), _Resp(429, headers={"Retry-After": "5"}), _Resp(200, {})]
    api.runTextCommand("SER", "TYPE", "CUST", "stop")
    assert slept == [2.0, 5.0] and len(api.seen) == 3
    assert all(m == "POST" and u.endswith("/api/behaviors/preview") for m, u, _ in api.seen)

    api.responses = [_Resp(429), _Resp(200, {"alarms": []})]       # the alarms backend too
    api.getAlarms()
    assert slept[-1] == 2.0


def test_429_gives_up_after_retries(api, monkeypatch):
    from pyalexaalerts.exception import AlexaAPIError
    slept = []
    monkeypatch.setattr("pyalexaalerts.api._time.sleep", slept.append)
    api.responses = [_Resp(429)] * 4
    with pytest.raises(AlexaAPIError, match="429"):
        api.runTextCommand("SER", "TYPE", "CUST", "stop")
    assert slept == [2.0, 4.0, 8.0] and len(api.seen) == 4


def test_alarm_401_remints_token(api, monkeypatch):
    minted = []
    monkeypatch.setattr("pyalexaalerts.auth.mint_access_token",
                        lambda rt: minted.append(rt) or ("Atna|fresh", datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc)))
    api.responses = [_Resp(401), _Resp(200, {"alarms": []})]
    api.getAlarms()
    assert minted == ["RT"]
    assert api.seen[-1][2]["headers"]["authorization"] == "Atna|fresh"


def test_reminder_and_timer_requests(api):
    api.responses = [_Resp(200, {"notifications": [{"id": "r1"}]})]
    assert api.getNotifications("REMINDER") == [{"id": "r1"}]
    method, url, kw = api.seen[-1]
    assert url == ("https://na-api-alexa.amazon.com/api/notifications?personId=amzn1.account.X"
                   "&notificationType=REMINDER&clientType=VOX")
    assert kw["headers"]["csrf"] == "tok123"

    api.getNotifications("TIMER")
    assert api.seen[-1][1].endswith("notificationType=TIMER")

    api.createReminder({"id": "null"})
    assert api.seen[-1][:2] == ("PUT", "https://na-api-alexa.amazon.com/api/notifications/null")

    api.deleteReminder("T-S-u_REMINDER")
    assert api.seen[-1][:2] == (
        "DELETE", "https://na-api-alexa.amazon.com/api/notifications/T-S-u_REMINDER?personId=amzn1.account.X&clientType=VOX")
    api.updateTimer({"id": "T-S-u", "status": "PAUSED"})
    assert api.seen[-1][:2] == ("PUT", "https://na-api-alexa.amazon.com/api/notifications/T-S-u")
    api.deleteTimer("T-S-u")
    assert api.seen[-1][:2] == ("DELETE", "https://na-api-alexa.amazon.com/api/notifications/T-S-u?personId=amzn1.account.X")


def test_sounds_query(api):
    api.getSounds("SER", "TYPE")
    assert api.seen[-1][1] == ("https://na-api-alexa.amazon.com/api/notification/sounds?deviceType=TYPE"
                               "&deviceSerialNumber=SER&softwareVersion=%27%27&alertType=Alarm")


def test_text_command(api):
    api.runTextCommand("SER", "TYPE", "CUST", "set a timer for 2 minutes")
    method, url, kw = api.seen[-1]
    assert (method, url) == ("POST", "https://na-api-alexa.amazon.com/api/behaviors/preview")
    seq = json.loads(kw["json"]["sequenceJson"])
    node = seq["startNode"]
    assert node["type"] == "Alexa.TextCommand" and node["skillId"] == "amzn1.ask.1p.tellalexa"
    assert node["operationPayload"] == {"deviceType": "TYPE", "deviceSerialNumber": "SER", "locale": "en-US",
                                        "customerId": "CUST", "text": "set a timer for 2 minutes"}
