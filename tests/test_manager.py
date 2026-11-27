import datetime

import pytest

from pyalexaalerts import (
    AlexaAlerts,
    DeviceNotFoundException,
    Kind,
    NotificationNotFoundException,
    Recurrence,
)
from pyalexaalerts import cli
from pyalexaalerts.manager import next_occurrence, resolve_when
from pyalexaalerts.resource import reminder_body, timer_phrase, Device

PID = "amzn1.account.TEST"
CAPS = ["TIMERS_AND_ALARMS", "REMINDERS"]
DEVICES = [
    {"serialNumber": "SER1", "deviceType": "TYPE1", "accountName": "Bedroom Echo", "deviceFamily": "ECHO",
     "online": True, "capabilities": CAPS, "deviceOwnerCustomerId": "CUST"},
    {"serialNumber": "SER2", "deviceType": "TYPE2", "accountName": "Kitchen Echo", "deviceFamily": "KNIGHT",
     "online": True, "capabilities": CAPS, "deviceOwnerCustomerId": "CUST"},
    {"serialNumber": "SER3", "deviceType": "TYPE3", "accountName": "Living Room Fire TV", "deviceFamily": "FIRE_TV",
     "online": False, "capabilities": []},
]
SOUNDS = [
    {"id": "system_alerts_melodic_01", "displayName": "Simple Alarm", "providerId": "ECHO", "folder": None},
    {"id": "system_alerts_atonal_02", "displayName": "Sonar", "providerId": "ECHO", "folder": None},
    {"id": "system_alerts_man_city_football", "displayName": "Man City Football", "providerId": "ECHO",
     "folder": "FOLDER_CELEBRITY"},
    {"id": "SKILL-amzn1.ask.skill.x", "displayName": "Paid Skill Sound", "providerId": "SKILL", "pricing": {"p": 1}},
]


def _alarm(token, serial="SER1", dtype="TYPE1", status="ON", date="2030-01-07", time="07:00", rec=None):
    trig = {"timeOfDay": time, "time": time, "date": date, "scheduledTime": f"{date}T{time}:00",
            "timeZoneId": "America/New_York"}
    if rec:
        trig["recurrence"] = rec
    return {"alarmToken": token, "status": status, "trigger": trig, "endpointIds": [f"{serial}@{dtype}"],
            "assets": [{"type": "TONE", "assetId": "system_alerts_melodic_01", "displayName": "Simple Alarm"}]}


def _reminder(idx, serial="SER2", label="Take out bins", date="2030-01-01", time="19:00:00.000", rrule=None,
              ntype="Reminder"):
    return {"id": f"TYPE2-{serial}-{idx}_REMINDER", "notificationIndex": idx, "type": ntype, "status": "ON",
            "deviceSerialNumber": serial, "deviceType": "TYPE2", "reminderLabel": label,
            "originalDate": date, "originalTime": time, "rRuleData": rrule, "alarmTime": 0, "version": "3",
            "personProfile": {"personId": PID}}


def _timer(idx, serial="SER2", ms=600000):
    return {"id": f"TYPE2-{serial}-{idx}", "notificationIndex": idx, "type": "Timer", "status": "ON",
            "deviceSerialNumber": serial, "deviceType": "TYPE2", "originalDurationInMillis": ms,
            "remainingTime": ms - 30000, "triggerTime": 1893481200000, "timerLabel": None}


class FakeAPI:
    personId = PID

    def __init__(self):
        self.alarms = [
            _alarm("aaaa1111-0000", rec={"freq": "WEEKLY", "byDay": ["MO", "TU", "WE", "TH", "FR"], "interval": 1}),
            _alarm("aaaa2222-0000", status="OFF"),
        ]
        self.notifs = {
            "REMINDER": [_reminder("bbbb1111-0000"), _reminder("todo0000", label="Buy milk", ntype="Task")],
            "TIMER": [_timer("cccc1111-0000")],
        }
        self.calls = []
        self.on_text_command = None

    def getDevices(self):
        return [dict(d) for d in DEVICES]

    def getSounds(self, serial, dtype, alert_type="Alarm"):
        return SOUNDS

    def getDefaultSound(self, serial, dtype, ntype="Alarm"):
        return {"defaultNotificationSound": SOUNDS[0]}

    def getAlarms(self):
        return [dict(a) for a in self.alarms]

    def createAlarm(self, body):
        self.calls.append(("createAlarm", body))
        serial, dtype = body["endpointId"].split("@")
        date, time = body["trigger"]["scheduledTime"].split("T")
        raw = _alarm("new0alarm-1", serial, dtype, date=date, time=time[:5], rec=body["trigger"].get("recurrence"))
        self.alarms.append(raw)
        return raw

    def updateAlarm(self, token, body):
        self.calls.append(("updateAlarm", token, body))
        old = next(a for a in self.alarms if a["alarmToken"] == token)
        date, time = body["trigger"]["scheduledTime"].split("T")
        serial, dtype = old["endpointIds"][0].split("@")
        return _alarm(token, serial, dtype, old["status"], date, time[:5], body["trigger"].get("recurrence"))

    def activateAlarm(self, token, body):
        self.calls.append(("activateAlarm", token, body))
        return dict(next(a for a in self.alarms if a["alarmToken"] == token), status="ON")

    def cancelAlarm(self, token):
        self.calls.append(("cancelAlarm", token))
        return dict(next(a for a in self.alarms if a["alarmToken"] == token), status="OFF")

    def deleteAlarm(self, token):
        self.calls.append(("deleteAlarm", token))
        self.alarms = [a for a in self.alarms if a["alarmToken"] != token]

    def getNotifications(self, ntype):
        return [dict(n) for n in self.notifs[ntype]]

    def createReminder(self, body):
        self.calls.append(("createReminder", body))
        raw = dict(body, id="TYPE2-SER2-new0rem-1_REMINDER", notificationIndex="new0rem-1")
        self.notifs["REMINDER"].append(raw)
        return raw

    def updateReminder(self, body):
        self.calls.append(("updateReminder", body))
        return dict(body)

    def deleteReminder(self, nid):
        self.calls.append(("deleteReminder", nid))

    def updateTimer(self, body):
        self.calls.append(("updateTimer", body))
        raw = dict(body, triggerTime=1893481300000 if body["status"] == "ON" else 0)
        self.notifs["TIMER"] = [raw if n["id"] == raw["id"] else n for n in self.notifs["TIMER"]]
        return raw

    def deleteTimer(self, nid):
        self.calls.append(("deleteTimer", nid))

    def runTextCommand(self, serial, dtype, customer, text):
        self.calls.append(("text", serial, dtype, customer, text))
        if self.on_text_command:
            self.on_text_command()


@pytest.fixture
def alexa():
    a = AlexaAlerts(api=FakeAPI())
    a.resync()
    return a


# ------------------------------------------------------------------ lookups

def test_devices_and_lookup(alexa):
    assert {d.name for d in alexa.devices(alarms=True)} == {"Bedroom Echo", "Kitchen Echo"}
    assert alexa.getDevice("SER2").name == "Kitchen Echo"
    assert alexa.getDevice("bedroom").serial == "SER1"
    with pytest.raises(DeviceNotFoundException, match="several"):
        alexa.getDevice("echo")
    with pytest.raises(DeviceNotFoundException, match="No device"):
        alexa.getDevice("garage")


def test_tasks_filtered_out(alexa):
    assert [r.label for r in alexa.find("reminder")] == ["Take out bins"]


def test_find_and_get(alexa):
    assert len(list(alexa.find("alarm"))) == 2
    assert len(list(alexa.find("alarm", enabled=True))) == 1
    assert len(list(alexa.find(device="kitchen"))) == 2  # reminder + timer
    assert alexa.get("bbbb").kind is Kind.REMINDER
    assert alexa.get("cccc").kind is Kind.TIMER
    assert alexa.get("aaaa1111").recurrence.describe() == "weekdays"
    with pytest.raises(NotificationNotFoundException, match="ambiguous"):
        alexa.get("aaaa")
    with pytest.raises(NotificationNotFoundException):
        alexa.get("zzzz")


def test_item_display(alexa):
    a = alexa.get("aaaa1111")
    assert a.time == datetime.datetime(2030, 1, 7, 7, 0) and a.deviceName == "Bedroom Echo"
    assert "weekdays" in str(a) and "[Simple Alarm]" in str(a)
    t = alexa.get("cccc")
    assert t.duration == datetime.timedelta(minutes=10)
    assert "0:09:30 left" in str(t)


# ------------------------------------------------------------------ alarms

def test_create_alarm_body_matches_app(alexa):
    a = alexa.createAlarm("bedroom", datetime.datetime(2030, 5, 6, 6, 45), recurrence="mon,wed,sun", sound="sonar")
    _, body = alexa.api.calls[-1]
    assert body == {
        "trigger": {"scheduledTime": "2030-05-06T06:45:00",
                    "recurrence": {"freq": "WEEKLY", "byDay": ["MO", "WE", "SU"], "interval": 1}},
        "endpointId": "SER1@TYPE1",
        "assets": [{"assetId": "system_alerts_atonal_02", "type": "TONE"}],
        "extensions": [{"name": "WAKE_UP_LIGHTS", "enablement": "DISABLED"}],
    }
    assert alexa.get(a.id) is a


def test_create_alarm_default_sound_daily_and_off(alexa):
    a = alexa.createAlarm("bedroom", datetime.time(7, 0), recurrence="daily", enabled=False)
    create = next(c for c in alexa.api.calls if c[0] == "createAlarm")[1]
    assert create["assets"] == [{"assetId": "system_alerts_melodic_01", "type": "TONE"}]
    assert create["trigger"]["recurrence"] == {"freq": "DAILY", "byDay": [], "interval": 1}
    assert alexa.api.calls[-1] == ("cancelAlarm", "new0alarm-1") and not a.enabled


def test_skill_and_unknown_sounds_rejected(alexa):
    with pytest.raises(ValueError, match="skill"):
        alexa.createAlarm("bedroom", datetime.time(7), sound="paid skill")
    with pytest.raises(ValueError, match="matches 0"):
        alexa.createAlarm("bedroom", datetime.time(7), sound="nope")


def test_enable_disable(alexa):
    a = alexa.disable("aaaa1111")
    assert not a.enabled and alexa.api.calls[-1] == ("cancelAlarm", "aaaa1111-0000")
    a = alexa.start(a)
    op, token, body = alexa.api.calls[-1]
    assert op == "activateAlarm" and a.enabled
    assert body == {"trigger": {"scheduledTime": "2030-01-07T07:00:00"},
                    "assets": [{"assetId": "system_alerts_melodic_01", "type": "TONE"}]}


def test_reminders_have_no_switch(alexa):
    with pytest.raises(ValueError, match="no on/off"):
        alexa.disable("bbbb")
    with pytest.raises(ValueError, match="only timers"):
        alexa.pause("bbbb1111")


def test_timer_pause_resume_bodies_match_app(alexa, monkeypatch):
    monkeypatch.setattr("pyalexaalerts.resource._time.time", lambda: 1893481200 - 100.0)
    alexa.api.notifs["TIMER"][0].update(deviceName="Kitchen", rRuleData=None, mrmGroupContext=None)
    alexa.refresh({Kind.TIMER})
    with pytest.raises(ValueError, match="pause"):
        alexa.disable("cccc")                   # timers have pause, not off
    t = alexa.pause("cccc")
    op, body = alexa.api.calls[-1]
    assert op == "updateTimer" and t.paused and t.enabled
    assert body["status"] == "PAUSED" and body["alarmTime"] == 1893481200000 and body["triggerTime"] == 0
    assert body["remainingTime"] == 100000
    assert body["mobileDsn"] is None and body["useRESTApi"] is False
    assert not {"deviceName", "rRuleData", "mrmGroupContext"} & body.keys()
    assert "paused" in str(t)
    assert alexa.pause(t) is t and len(alexa.api.calls) == 1  # already paused: no request

    t = alexa.resume("cccc")
    op, body = alexa.api.calls[-1]
    assert (body["status"], body["alarmTime"], body["triggerTime"], body["remainingTime"]) == ("ON", 0, 0, 100000)
    assert not t.paused and t.time is not None


def test_update_alarm_time_and_repeat(alexa):
    a = alexa.updateAlarm("aaaa2222", when=datetime.datetime(2030, 2, 1, 6, 20))
    _, token, body = alexa.api.calls[-1]
    assert token == "aaaa2222-0000"
    assert body == {"trigger": {"scheduledTime": "2030-02-01T06:20:00"},
                    "assets": [{"assetId": "system_alerts_melodic_01", "type": "TONE"}], "extensions": []}
    a = alexa.update(a, recurrence="weekends")
    body = alexa.api.calls[-1][2]
    assert body["trigger"]["recurrence"] == {"freq": "WEEKLY", "byDay": ["SA", "SU"], "interval": 1}
    assert datetime.date.fromisoformat(body["trigger"]["scheduledTime"][:10]).weekday() >= 5  # moved to a weekend
    alexa.update(a, sound="Man City")
    assert alexa.api.calls[-1][2]["assets"] == [{"assetId": "system_alerts_man_city_football", "type": "TONE"}]


def test_delete_dispatches(alexa):
    alexa.delete("aaaa2222")
    alexa.delete("bbbb")
    alexa.delete("cccc")
    assert [c[0] for c in alexa.api.calls] == ["deleteAlarm", "deleteReminder", "deleteTimer"]
    assert alexa.api.calls[1][1] == "TYPE2-SER2-bbbb1111-0000_REMINDER"
    with pytest.raises(NotificationNotFoundException):
        alexa.get("bbbb")


# ------------------------------------------------------------------ reminders

def test_create_recurring_reminder_matches_app(alexa):
    alexa.createReminder("kitchen", datetime.time(21, 0), "Take out trash", recurrence="tue,thu")
    body = alexa.api.calls[-1][1]
    assert body["id"] == "null" and body["type"] == "Reminder" and body["status"] == "ON"
    assert body["deviceSerialNumber"] == "SER2" and body["deviceType"] == "TYPE2"
    assert body["reminderLabel"] == "Take out trash" and body["deliveryType"] == "SPOKEN_DEVICE"
    assert body["clientType"] == "VOX" and body["assigner"] == {"person": {"id": PID}}
    assert body["personProfile"]["personId"] == PID
    rr = body["rRuleData"]
    assert rr["byWeekDays"] == ["TU", "TH"] and rr["notificationTimes"] == ["21:00:00.000"]
    assert rr["frequency"] == "WEEKLY" and rr["flexibleRecurringPatternType"] == "EVERY_X_WEEKS"
    assert rr["recurStartDate"] == datetime.date.today().isoformat() and rr["intervals"] == [1]
    assert body["originalDate"] is None and body["alarmTime"] == 0


def test_create_one_off_reminder(alexa):
    when = datetime.datetime(2030, 5, 6, 18, 30)
    r = alexa.createReminder("kitchen", when, "Pizza")
    body = alexa.api.calls[-1][1]
    assert body["rRuleData"] is None
    assert (body["originalDate"], body["originalTime"]) == ("2030-05-06", "18:30:00.000")
    assert body["alarmTime"] == int(when.timestamp() * 1000)
    assert r.label == "Pizza" and r.kind is Kind.REMINDER
    with pytest.raises(ValueError):
        alexa.createReminder("kitchen", when, "  ")


def test_daily_reminder_is_every_weekday():
    body = reminder_body(Device({"serialNumber": "S", "deviceType": "T"}), datetime.datetime(2030, 1, 1, 8),
                         "Vitamins", Recurrence.parse("daily"), PID, today=datetime.date(2030, 1, 1))
    assert body["rRuleData"]["byWeekDays"] == ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def test_update_reminder(alexa):
    r = alexa.updateReminder("bbbb", text="Bins!", when=datetime.datetime(2030, 3, 3, 20, 0))
    body = alexa.api.calls[-1][1]
    assert body["id"] == "TYPE2-SER2-bbbb1111-0000_REMINDER" and body["status"] == "ON"
    assert body["reminderLabel"] == "Bins!" and body["originalDate"] == "2030-03-03"
    assert body["clientType"] == "VOX" and r.label == "Bins!"


# ------------------------------------------------------------------ timers

def test_create_timer(alexa, monkeypatch):
    monkeypatch.setattr("pyalexaalerts.manager._time.sleep", lambda s: None)
    api = alexa.api
    api.on_text_command = lambda: api.notifs["TIMER"].append(_timer("dddd2222-0000", ms=120000))
    t = alexa.createTimer("kitchen", datetime.timedelta(minutes=2), wait=5)
    assert ("text", "SER2", "TYPE2", "CUST", "set a timer for 2 minutes") in api.calls
    assert t.shortId == "dddd2222" and t.duration == datetime.timedelta(minutes=2)


def test_adjust_running_timer_pauses_with_new_time_then_resumes(alexa, monkeypatch):
    monkeypatch.setattr("pyalexaalerts.resource._time.time", lambda: 1893481200 - 100.0)   # 100 s left
    t = alexa.adjustTimer("cccc", datetime.timedelta(minutes=5))
    (op1, pause), (op2, resume) = alexa.api.calls[-2:]
    assert (op1, pause["status"], pause["remainingTime"], pause["triggerTime"]) == ("updateTimer", "PAUSED", 400000, 0)
    assert (op2, resume["status"], resume["remainingTime"], resume["triggerTime"]) == ("updateTimer", "ON", 400000, 0)
    assert not t.paused and t.time is not None
    assert not any(c[0] == "text" for c in alexa.api.calls)                                 # no voice command

    alexa.adjustTimer("cccc", -datetime.timedelta(minutes=1))      # fake resume rang at base+100 s: 200 s left
    assert alexa.api.calls[-2][1]["remainingTime"] == 140000
    with pytest.raises(ValueError, match="only has"):
        alexa.adjustTimer("cccc", -datetime.timedelta(hours=1))


def test_subtract_from_running_timer(alexa, monkeypatch):
    monkeypatch.setattr("pyalexaalerts.resource._time.time", lambda: 1893481200 - 300.0)   # 5 min left
    alexa.adjustTimer("cccc", -datetime.timedelta(minutes=2))
    assert alexa.api.calls[-2][1]["remainingTime"] == 180000


def test_adjust_paused_timer_stays_paused(alexa, monkeypatch):
    monkeypatch.setattr("pyalexaalerts.resource._time.time", lambda: 1893481200 - 100.0)
    alexa.pause("cccc")
    n = len(alexa.api.calls)
    t = alexa.adjustTimer("cccc", datetime.timedelta(seconds=30))
    assert len(alexa.api.calls) == n + 1                      # one PUT, no resume
    body = alexa.api.calls[-1][1]
    assert (body["status"], body["remainingTime"]) == ("PAUSED", 130000)
    assert t.paused


def test_adjust_refuses_non_timers(alexa):
    with pytest.raises(ValueError, match="only timers"):
        alexa.adjustTimer("bbbb", datetime.timedelta(minutes=1))


def test_cli_add_subtract_time(fake_cli, capsys, monkeypatch):
    monkeypatch.setattr("pyalexaalerts.resource._time.time", lambda: 1893481200 - 300.0)
    assert cli.main(["add-time", "cccc", "5m"]) == 0
    assert "Added 5:00" in capsys.readouterr().out
    assert fake_cli.calls[-2][1]["remainingTime"] == 600000
    assert cli.main(["subtract-time", "cccc", "1h"]) == 2     # more than is left


def test_create_timer_times_out(alexa, monkeypatch):
    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr("pyalexaalerts.manager._time.monotonic", lambda: next(clock))
    monkeypatch.setattr("pyalexaalerts.manager._time.sleep", lambda s: None)
    assert alexa.createTimer("kitchen", datetime.timedelta(minutes=2), wait=10) is None


@pytest.mark.parametrize("delta,label,phrase", [
    (datetime.timedelta(minutes=2), None, "set a timer for 2 minutes"),
    (datetime.timedelta(hours=1, minutes=30), None, "set a timer for 1 hour and 30 minutes"),
    (datetime.timedelta(hours=1, minutes=1, seconds=5), None, "set a timer for 1 hour, 1 minute and 5 seconds"),
    (datetime.timedelta(seconds=90), "pizza", "set a pizza timer for 1 minute and 30 seconds"),
])
def test_timer_phrase(delta, label, phrase):
    assert timer_phrase(delta, label) == phrase


# ------------------------------------------------------------------ helpers

@pytest.mark.parametrize("value,expected", [
    ("daily", Recurrence("DAILY")), ("Weekdays", Recurrence("WEEKLY", ("MO", "TU", "WE", "TH", "FR"))),
    ("weekends", Recurrence("WEEKLY", ("SA", "SU"))), ("sun,mon,WED", Recurrence("WEEKLY", ("MO", "WE", "SU"))),
    ("monday tuesday wednesday thursday friday saturday sunday", Recurrence("DAILY")),
    ("none", None), (None, None),
])
def test_recurrence_parse(value, expected):
    assert Recurrence.parse(value) == expected


def test_recurrence_rejects_junk():
    with pytest.raises(ValueError):
        Recurrence.parse("fortnightly")


def test_first_occurrence_and_resolve_when():
    now = datetime.datetime(2030, 1, 1, 12, 0)  # a Tuesday
    assert next_occurrence(datetime.time(13), now) == datetime.datetime(2030, 1, 1, 13)
    assert next_occurrence(datetime.time(11), now) == datetime.datetime(2030, 1, 2, 11)
    weekends = Recurrence.parse("weekends")
    assert resolve_when(datetime.time(7), weekends, now) == datetime.datetime(2030, 1, 5, 7)
    assert resolve_when(datetime.timedelta(minutes=20), None, now) == datetime.datetime(2030, 1, 1, 12, 20)


@pytest.mark.parametrize("value,expected", [
    ("07:30", datetime.time(7, 30)), ("7:30pm", datetime.time(19, 30)), ("7pm", datetime.time(19)),
    ("7 PM", datetime.time(19)), ("19:05", datetime.time(19, 5)), ("7:30 a.m.", datetime.time(7, 30)),
])
def test_cli_parse_time(value, expected):
    assert cli.parse_time(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("20m", datetime.timedelta(minutes=20)), ("1h30m", datetime.timedelta(hours=1, minutes=30)),
    ("90s", datetime.timedelta(seconds=90)), ("45", datetime.timedelta(minutes=45)),
])
def test_cli_parse_duration(value, expected):
    assert cli.parse_duration(value) == expected


# ------------------------------------------------------------------ CLI end-to-end (fake API)

@pytest.fixture
def fake_cli(monkeypatch):
    api = FakeAPI()
    monkeypatch.setattr(cli, "AlexaAlerts", lambda **kw: AlexaAlerts(api=api))
    monkeypatch.setattr("pyalexaalerts.manager._time.sleep", lambda s: None)
    return api


def test_cli_list(fake_cli, capsys):
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "aaaa1111" in out and "bbbb1111" in out and "cccc1111" in out
    assert "aaaa2222" not in out and "Buy milk" not in out  # OFF alarm hidden, tasks excluded
    assert cli.main(["list", "--all", "--type", "alarm"]) == 0
    out = capsys.readouterr().out
    assert "aaaa2222" in out and "bbbb1111" not in out


def test_snoozed_reminder_counts_as_active(alexa):
    alexa.api.notifs["REMINDER"][0]["status"] = "SNOOZED"     
    alexa.refresh({Kind.REMINDER})
    rem = alexa.get("bbbb")
    assert rem.enabled and rem.status == "SNOOZED"
    assert rem in list(alexa.find("reminder", enabled=True))   # shown by plain `list`


def test_tasks_listed_only_when_asked(alexa):
    assert all(i.kind is not Kind.TASK for i in alexa.find())           # hidden by default
    (task,) = alexa.find("task")
    assert (task.label, task.status, task.enabled, task.deviceName) == ("Buy milk", "TO DO", True, "")
    assert list(alexa.find("todos")) == [task]                          # alias
    assert str(task) == 'task todo0000 "Buy milk"'


def test_task_due_date_note_and_done(alexa):
    alexa.api.notifs["REMINDER"][1].update(status="OFF", taskMetadata={
        "note": "2%", "flagged": True, "dueDate": "2030-01-05", "dueTime": None})
    alexa.refresh({Kind.TASK})
    (task,) = alexa.find("task")
    assert (task.done, task.enabled, task.status) == (True, False, "DONE")
    assert task.when_str() == "due Sat 2030-01-05"
    assert task.label_str() == '"Buy milk" [important] (note: 2%)'
    assert list(alexa.find("task", enabled=True)) == []


def test_tasks_are_read_only(alexa):
    n = len(alexa.api.calls)
    with pytest.raises(ValueError, match="to-do item"):
        alexa.delete("todo")                                             # found by prefix, refused
    (task,) = alexa.find("task")
    for action in (alexa.delete, alexa.enable, alexa.disable, lambda t: alexa.update(t, text="x")):
        with pytest.raises(ValueError, match="to-do item"):
            action(task)
    assert len(alexa.api.calls) == n                                     # nothing was sent


def test_cli_list_tasks(fake_cli, capsys):
    assert cli.main(["list"]) == 0
    assert "Buy milk" not in capsys.readouterr().out
    assert cli.main(["list", "--type", "tasks"]) == 0
    out = capsys.readouterr().out
    assert "Buy milk" in out and "TO DO" in out
    assert cli.main(["list", "-T", "todo"]) == 0
    assert cli.main(["delete", "todo", "-y"]) == 2


def test_stop_and_snooze_send_commands(alexa):
    alexa.stopAlert("kitchen")
    alexa.snooze("kitchen")
    alexa.snooze("kitchen", datetime.timedelta(minutes=5))
    alexa.snooze("kitchen", datetime.timedelta(minutes=1))
    assert [c[-1] for c in alexa.api.calls if c[0] == "text"] == [
        "stop", "snooze", "snooze for 5 minutes", "snooze for 1 minute"]
    with pytest.raises(ValueError):
        alexa.snooze("kitchen", datetime.timedelta(hours=2))


def test_cli_stop_snooze(fake_cli, capsys):
    assert cli.main(["stop-alert", "-d", "kitchen"]) == 0
    assert cli.main(["snooze", "-d", "kitchen", "--for", "10m"]) == 0
    assert [c[-1] for c in fake_cli.calls if c[0] == "text"][-2:] == ["stop", "snooze for 10 minutes"]
    with pytest.raises(SystemExit):                                        
        cli.main(["stop-alert", "-d", "kitchen", "--direct"])


def test_cli_timer_sounds(fake_cli, capsys):
    assert cli.main(["sounds", "-d", "kitchen", "--for", "timer"]) == 0
    assert "current default" in capsys.readouterr().out


def test_cli_sounds(fake_cli, capsys):
    assert cli.main(["sounds", "-d", "bedroom"]) == 0
    out = capsys.readouterr().out
    assert "Sonar" in out and "skill (unsupported)" in out


def test_cli_alarm_one_request_for_many_days(fake_cli):
    assert cli.main(["alarm", "-d", "bedroom", "-t", "7:00", "-r", "mon,wed,fri", "-s", "sonar"]) == 0
    creates = [c[1] for c in fake_cli.calls if c[0] == "createAlarm"]
    assert len(creates) == 1
    assert creates[0]["trigger"]["recurrence"]["byDay"] == ["MO", "WE", "FR"]


def test_cli_reminder_and_timer(fake_cli, capsys):
    assert cli.main(["reminder", "-d", "kitchen", "--in", "20m", "-m", "Pizza"]) == 0
    fake_cli.on_text_command = lambda: fake_cli.notifs["TIMER"].append(_timer("eeee", ms=600000))
    assert cli.main(["timer", "-d", "kitchen", "10m"]) == 0
    assert "Created timer eeee" in capsys.readouterr().out


def test_cli_off_on_edit_delete(fake_cli, capsys):
    assert cli.main(["off", "aaaa1111"]) == 0
    assert fake_cli.calls[-1][0] == "cancelAlarm"
    assert cli.main(["on", "aaaa1111"]) == 0
    assert fake_cli.calls[-1][0] == "activateAlarm"
    assert cli.main(["pause", "cccc"]) == 0
    assert fake_cli.calls[-1][1]["status"] == "PAUSED" and "Paused" in capsys.readouterr().out
    assert cli.main(["resume", "cccc"]) == 0
    assert fake_cli.calls[-1][1]["status"] == "ON" and "Resumed" in capsys.readouterr().out
    assert cli.main(["edit", "aaaa1111", "-r", "none"]) == 0
    assert "recurrence" not in fake_cli.calls[-1][2]["trigger"]
    assert cli.main(["edit", "bbbb", "-m", "New text"]) == 0
    assert fake_cli.calls[-1][1]["reminderLabel"] == "New text"
    assert cli.main(["delete", "-y", "bbbb"]) == 0
    assert fake_cli.calls[-1][0] == "deleteReminder"
    assert cli.main(["cancel", "-y", "cccc"]) == 0   # cancel = delete
    assert fake_cli.calls[-1][0] == "deleteTimer"


def test_cli_errors(fake_cli, capsys):
    assert cli.main(["off", "zzzz"]) == 2
    assert cli.main(["off", "bbbb"]) == 2     # reminders have no switch
    assert cli.main(["off", "cccc"]) == 2     # timers: pause or delete
    assert cli.main(["pause", "bbbb"]) == 2   # only timers pause
    assert cli.main(["alarm", "-d", "garage", "-t", "7:00"]) == 2
    assert cli.main(["edit", "aaaa1111", "-m", "text"]) == 2
    assert "error:" in capsys.readouterr().err


# rRuleData shapes as returned by the server for reminders made in the app / by this library
_WEEKLY_EDITED = {"byWeekDays": ["TU", "TH"], "flexibleRecurringPatternType": "EVERY_X_WEEKS", "frequency": None,
                  "nextTriggerTimes": ["2026-12-01T21:15:00.000-05:00"], "notificationTimes": ["21:15:00.000"] * 2}
_DAILY = {"byWeekDays": [], "flexibleRecurringPatternType": "ONCE_A_DAY", "notificationTimes": ["07:30:00.000"],
          "nextTriggerTimes": ["2026-11-27T07:30:00.000-05:00"],
          "recurrenceRules": ["FREQ=DAILY;BYHOUR=7;BYMINUTE=30;BYSECOND=0;INTERVAL=1;"]}
_MONTHLY = {"byMonthDays": [2], "byWeekDays": [], "flexibleRecurringPatternType": "EVERY_X_MONTHS",
            "notificationTimes": ["19:00:00.000"], "nextTriggerTimes": ["2026-12-02T19:00:00.000-05:00"],
            "recurrenceRules": ["FREQ=MONTHLY;BYMONTHDAY=2;BYHOUR=19;BYMINUTE=0;BYSECOND=0;INTERVAL=1;"]}


@pytest.mark.parametrize("rrule, original_time, alarm_ms, when, repeat", [
    (_WEEKLY_EDITED, None, 1796169600000, datetime.datetime(2026, 12, 1, 21, 15), "Tue,Thu"),
    (_DAILY, "07:30:00.000", 1795782600000, datetime.datetime(2026, 11, 27, 7, 30), "daily"),
    (_MONTHLY, None, 1796256000000, datetime.datetime(2026, 12, 2, 19, 0), "monthly on 2nd"),
])
def test_reminder_time_and_repeat_from_server_shapes(rrule, original_time, alarm_ms, when, repeat):
    from pyalexaalerts import Reminder
    r = Reminder({"rRuleData": rrule, "originalDate": "2026-01-12", "originalTime": original_time,
                  "alarmTime": alarm_ms, "reminderLabel": "x"})
    assert r.time == when and r.recurrence.describe() == repeat


def test_one_off_reminder_time_ignores_empty_rrule():
    from pyalexaalerts import Reminder
    r = Reminder({"originalDate": "2026-11-26", "originalTime": "21:34:19.000", "alarmTime": 1,
                  "rRuleData": {"notificationTimes": None, "byWeekDays": None, "flexibleRecurringPatternType": None}})
    assert r.time == datetime.datetime(2026, 11, 26, 21, 34, 19) and r.recurrence is None


@pytest.mark.parametrize("value, days", [
    ("monthly", ()), ("Monthly:15", ("15",)), ("monthly on the 2nd", ("2",)), ("monthly:15,15", ("15",)),
])
def test_parse_monthly(value, days):
    assert Recurrence.parse(value) == Recurrence("MONTHLY", days)


@pytest.mark.parametrize("bad", ["monthly:0", "monthly:32", "monthly:x", "monthly:1,15"])
def test_parse_monthly_rejects(bad):
    with pytest.raises(ValueError):
        Recurrence.parse(bad)


def _monthly_rrule(body):
    r = body["rRuleData"]
    return (r["flexibleRecurringPatternType"], r["frequency"], r["byMonthDays"], r["byWeekDays"],
            r["notificationTimes"], r["intervals"], body["alarmTime"], body["originalDate"])


def test_create_monthly_reminder(alexa):
    rem = alexa.createReminder("kitchen", datetime.time(19), "Bella medicine", recurrence="monthly:2")
    _, body = alexa.api.calls[-1]
    assert _monthly_rrule(body) == ("EVERY_X_MONTHS", "MONTHLY", [2], [], ["19:00:00.000"], [1], 0, None)
    assert rem.recurrence.describe() == "monthly on 2nd"


def test_bare_monthly_uses_day_of_first_occurrence(alexa):
    alexa.createReminder("kitchen", datetime.datetime(2030, 1, 17, 8, 0), "Rent", recurrence="monthly")
    assert alexa.api.calls[-1][1]["rRuleData"]["byMonthDays"] == [17]


def test_monthly_first_occurrence_skips_short_months():
    rec = Recurrence.parse("monthly:31")
    nxt = rec.first_occurrence(datetime.time(9), now=datetime.datetime(2030, 2, 1))
    assert nxt == datetime.datetime(2030, 3, 31, 9)


def test_edit_existing_monthly_reminder_rebuilds_monthly(alexa):
    alexa.api.notifs["REMINDER"][0]["rRuleData"] = dict(_MONTHLY)
    alexa.refresh({Kind.REMINDER})
    alexa.updateReminder("bbbb", text="new text")                 # text-only: same monthly rule, rebuilt
    body = alexa.api.calls[-1][1]
    assert _monthly_rrule(body) == ("EVERY_X_MONTHS", "MONTHLY", [2], [], ["19:00:00.000"], [1], 0, None)
    alexa.updateReminder("bbbb", when=datetime.time(20, 30))       # new time, still the 2nd
    assert alexa.api.calls[-1][1]["rRuleData"]["notificationTimes"] == ["20:30:00.000"]
    alexa.updateReminder("bbbb", recurrence="monthly:15")          # move to another day
    assert alexa.api.calls[-1][1]["rRuleData"]["byMonthDays"] == [15]
    alexa.updateReminder("bbbb", recurrence="weekdays")            # replace with weekly
    assert alexa.api.calls[-1][1]["rRuleData"]["byWeekDays"] == ["MO", "TU", "WE", "TH", "FR"]


def test_alarms_refuse_monthly(alexa):
    with pytest.raises(ValueError, match="alarms can't repeat monthly"):
        alexa.createAlarm("bedroom", datetime.time(7), recurrence="monthly:2")


def test_cli_monthly_with_start_date(fake_cli):
    assert cli.main(["reminder", "-d", "kitchen", "-t", "9am", "-D", "2030-01-15", "-r", "monthly",
                     "-m", "Pay rent"]) == 0
    assert fake_cli.calls[-1][1]["rRuleData"]["byMonthDays"] == [15]
    assert cli.main(["reminder", "-d", "kitchen", "-t", "9am", "-D", "2030-01-15", "-r", "monthly:2",
                     "-m", "x"]) == 2   # date not on a repeat day
    assert cli.main(["reminder", "-d", "kitchen", "-t", "9am", "-D", "2030-01-15", "-r", "mon",
                     "-m", "x"]) == 2   # weekly + date still refused


def test_text_only_edit_of_repeating_reminder_rebuilds_schedule(alexa):
    # the server's stored shape; sending this back as-is made Alexa turn it into a one-off
    stored = dict(_WEEKLY_EDITED, intervals=[1, 1], recurStartDate="2026-11-26",
                  recurrenceRules=["FREQ=WEEKLY;BYDAY=TU;BYHOUR=21;BYMINUTE=15;BYSECOND=0;INTERVAL=1;"] * 2)
    alexa.api.notifs["REMINDER"][0].update(rRuleData=stored, alarmTime=1796169600000, originalDate="2026-12-01",
                                           originalTime=None)
    alexa.refresh({Kind.REMINDER})
    alexa.updateReminder("bbbb", text="renamed")
    body = alexa.api.calls[-1][1]
    assert body["reminderLabel"] == "renamed"
    assert (body["alarmTime"], body["originalDate"], body["originalTime"]) == (0, None, None)
    r = body["rRuleData"]
    assert r["frequency"] == "WEEKLY" and r["byWeekDays"] == ["TU", "TH"] and r["intervals"] == [1]
    assert r["notificationTimes"] == ["21:15:00.000"] and r["nextTriggerTimes"] == [] and "recurrenceRules" not in r


def test_cli_unquote():
    assert cli._unquote('"Take medicine"') == "Take medicine"
    assert cli._unquote('"Pasta" 1:00:00 total') == '"Pasta" 1:00:00 total'
    assert cli._unquote("[Clangy]") == "[Clangy]"


def _weekly_series(alexa):
    alexa.api.notifs["REMINDER"][0].update(
        rRuleData=dict(_WEEKLY_EDITED, nextTriggerTimes=["2030-01-08T21:15:00.000-05:00"]),
        alarmTime=1893974400000, originalDate="2030-01-08", originalTime=None, version="3",
        createdDate=111, lastUpdatedDate=222, taskMetadata={"overdueAt": 1894002000000},
        assigner={"person": {"id": PID, "idV2": "amzn1.actor.person.did.X"}})

    def split(body):
        alexa.api.calls.append(("updateReminder", body))
        if body["status"] != "INSTANCE_UPDATED":
            return dict(body)
        one_off = dict(body, id="TYPE2-SER2-split000-1_REMINDER", notificationIndex="split000-1", status="ON",
                       rRuleData={"byWeekDays": None, "notificationTimes": None})
        alexa.api.notifs["REMINDER"].append(one_off)
        return one_off
    alexa.api.updateReminder = split
    alexa.refresh({Kind.REMINDER})


def test_next_occurrence_edit_matches_captured_shape(alexa):
    _weekly_series(alexa)
    new = alexa.updateReminderOccurrence("bbbb1111", when=datetime.time(22, 0), text="Just this once")
    _, body = alexa.api.calls[-1]
    # captured: series id, INSTANCE_UPDATED, series alarmTime/version, new originalDate/Time, no rRuleData
    assert body["id"] == alexa.api.notifs["REMINDER"][0]["id"] and body["status"] == "INSTANCE_UPDATED"
    assert (body["alarmTime"], body["version"], body["createdDate"], body["lastUpdatedDate"]) == (1893974400000, "3", 111, 222)
    assert (body["originalDate"], body["originalTime"]) == ("2030-01-08", "22:00:00.000")
    assert "rRuleData" not in body and body["clientType"] == "VOX"
    assert body["reminderLabel"] == "Just this once" and body["taskMetadata"]["overdueAt"] == 1894002000000
    assert body["assigner"]["person"]["idV2"] == "amzn1.actor.person.did.X"
    assert new.shortId == "split000" and new.recurrence is None
    assert new.time == datetime.datetime(2030, 1, 8, 22, 0)


def test_reminder_note_important_keep_reminding(alexa):
    rem = alexa.createReminder("kitchen", datetime.datetime(2030, 1, 5, 9), "Pay rent", note="  check the portal ",
                               important=True, keep_reminding=True)
    body = alexa.api.calls[-1][1]
    assert body["taskMetadata"]["note"] == "check the portal" and body["taskMetadata"]["flagged"] is True
    assert body["followUpMetadata"] == {"requiresFollowUp": True, "followUpStatus": "UNACKNOWLEDGED"}
    assert (rem.note, rem.important, rem.keepReminding) == ("check the portal", True, True)
    assert '"Pay rent" [important] [keep reminding] (note: check the portal)' in str(rem)

    plain = alexa.createReminder("kitchen", datetime.datetime(2030, 1, 5, 10), "Plain")
    assert (plain.note, plain.important, plain.keepReminding) == (None, False, False)
    assert alexa.api.calls[-1][1]["taskMetadata"]["flagged"] is False


def test_update_reminder_note_and_flags(alexa):
    alexa.updateReminder("bbbb", note="bring the ID", important=True, keep_reminding=True)
    body = alexa.api.calls[-1][1]
    assert (body["taskMetadata"]["note"], body["taskMetadata"]["flagged"]) == ("bring the ID", True)
    assert body["followUpMetadata"]["requiresFollowUp"] is True
    assert body["reminderLabel"] == "Take out bins"
    alexa.api.notifs["REMINDER"][0].update(taskMetadata=body["taskMetadata"], followUpMetadata=body["followUpMetadata"])
    alexa.refresh({Kind.REMINDER})
    alexa.updateReminder("bbbb", note=None, keep_reminding=False)    # important untouched
    body = alexa.api.calls[-1][1]
    assert (body["taskMetadata"]["note"], body["taskMetadata"]["flagged"]) == (None, True)
    assert body["followUpMetadata"]["requiresFollowUp"] is False


def test_next_occurrence_keeps_series_note_and_flags(alexa):
    _weekly_series(alexa)
    raw = alexa.api.notifs["REMINDER"][0]
    raw["taskMetadata"] = {**raw.get("taskMetadata", {}), "note": "n", "flagged": True}
    raw["followUpMetadata"] = {"requiresFollowUp": True, "followUpStatus": "UNACKNOWLEDGED"}
    alexa.refresh({Kind.REMINDER})
    alexa.updateReminderOccurrence("bbbb1111", when=datetime.time(22, 0))
    body = alexa.api.calls[-1][1]
    assert (body["taskMetadata"]["note"], body["taskMetadata"]["flagged"]) == ("n", True)
    assert body["followUpMetadata"]["requiresFollowUp"] is True


def test_cli_reminder_note_flags(fake_cli, capsys):
    assert cli.main(["reminder", "-d", "kitchen", "--in", "1h", "-m", "Call mom", "-n", "about Sunday",
                     "--important", "--keep-reminding"]) == 0
    body = fake_cli.calls[-1][1]
    assert (body["taskMetadata"]["note"], body["taskMetadata"]["flagged"]) == ("about Sunday", True)
    assert body["followUpMetadata"]["requiresFollowUp"] is True
    assert cli.main(["edit", "bbbb", "--no-important", "--note", "none"]) == 0
    body = fake_cli.calls[-1][1]
    assert (body["taskMetadata"]["note"], body["taskMetadata"]["flagged"]) == (None, False)
    assert "requiresFollowUp" not in (body.get("followUpMetadata") or {})   # untouched
    assert cli.main(["edit", "aaaa", "--important"]) == 2                   # alarm: refused
    assert cli.main(["edit", "bbbb", "--next-only", "--important"]) == 2


def test_next_occurrence_date_only_keeps_time_and_text(alexa):
    _weekly_series(alexa)
    alexa.updateReminderOccurrence("bbbb1111", when=datetime.date(2030, 1, 9))
    _, body = alexa.api.calls[-1]
    assert (body["originalDate"], body["originalTime"]) == ("2030-01-09", "21:15:00.000")
    assert body["reminderLabel"] == alexa.get("bbbb1111").text


def test_next_occurrence_edit_rejects_one_offs_and_noops(alexa):
    with pytest.raises(ValueError, match="only repeating"):
        alexa.api.notifs["REMINDER"][0]["rRuleData"] = None
        alexa.refresh({Kind.REMINDER})
        alexa.updateReminderOccurrence("bbbb1111", when=datetime.time(9))
    _weekly_series(alexa)
    with pytest.raises(ValueError, match="nothing to change"):
        alexa.updateReminderOccurrence("bbbb1111")


def test_cli_edit_next_only(fake_cli, capsys, monkeypatch):
    s = fake_cli.notifs["REMINDER"][0]
    s.update(rRuleData=dict(_WEEKLY_EDITED, nextTriggerTimes=["2030-01-08T21:15:00.000-05:00"]), alarmTime=1893974400000)
    assert cli.main(["edit", "bbbb", "--next-only", "-t", "10pm"]) == 0
    body = fake_cli.calls[-1][1]
    assert body["status"] == "INSTANCE_UPDATED" and body["originalTime"] == "22:00:00.000"
    assert "Changed the Tue 2030-01-08 21:15 occurrence" in capsys.readouterr().out
    assert cli.main(["edit", "bbbb", "--next-only", "-r", "daily"]) == 2


def test_rung_one_off_reminder_shows_done_and_phone_device():
    from pyalexaalerts import Reminder
    r = Reminder({"status": "OFF", "lastTriggerTimeInUtc": "2026-11-27T02:34:19.040Z", "originalDate": "2026-11-26",
                  "originalTime": "21:34:19.000", "rRuleData": {"notificationTimes": None},
                  "deviceName": "OTHER_MOBILE_DEVICE", "deviceSerialNumber": "99e234"})
    assert r.status == "DONE" and not r.enabled and r.deviceName == "Alexa app (phone)"


def test_multi_day_monthly_refused(alexa, fake_cli=None):
    with pytest.raises(ValueError, match="one day of the month"):
        alexa.createReminder("kitchen", datetime.time(9), "Bills", recurrence="monthly:1,15")
    assert not any(c[0] == "createReminder" for c in alexa.api.calls)
