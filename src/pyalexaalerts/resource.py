"""Models for devices, alarms, reminders, timers and sounds, plus request-body builders."""
import copy
import datetime
import re
import time as _time
from dataclasses import dataclass
from enum import Enum

DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_DAY_NAMES = {
    "mo": "MO", "mon": "MO", "monday": "MO",
    "tu": "TU", "tue": "TU", "tues": "TU", "tuesday": "TU",
    "we": "WE", "wed": "WE", "wednesday": "WE",
    "th": "TH", "thu": "TH", "thur": "TH", "thurs": "TH", "thursday": "TH",
    "fr": "FR", "fri": "FR", "friday": "FR",
    "sa": "SA", "sat": "SA", "saturday": "SA",
    "su": "SU", "sun": "SU", "sunday": "SU",
}
_DAY_LABEL = dict(zip(DAYS, ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")))


class Kind(str, Enum):
    ALARM = "alarm"
    REMINDER = "reminder"
    TIMER = "timer"
    TASK = "task"  # Alexa to-do items: only listed, and only when asked for

    @classmethod
    def from_str(cls, value: "str | Kind") -> "Kind":
        if isinstance(value, Kind):
            return value
        v = value.strip().lower().rstrip("s")
        try:
            return cls({"todo": "task", "to-do": "task"}.get(v, v))
        except ValueError:
            raise ValueError(f"Unknown type '{value}' (alarm, reminder, timer or task)") from None


@dataclass(frozen=True)
class Recurrence:
    """A repeat rule: DAILY, WEEKLY on specific days (MO..SU), or MONTHLY on days of the month.

    MONTHLY days hold one day-of-month number as a string ("15"); empty means "the day of the first
    occurrence" and is filled in when the reminder is built. MONTHLY is for reminders only, and only one
    day: Amazon silently keeps just the first of several days
    """

    freq: str
    days: tuple = ()

    @classmethod
    def parse(cls, value: "str | Recurrence | None") -> "Recurrence | None":
        """Parse 'daily', 'weekdays', 'weekends', 'mon,wed,sun' (any day spelling), 'monthly',
        'monthly:15' / 'monthly on the 2nd'; 'none'/'once' → None."""
        if value is None or isinstance(value, Recurrence):
            return value
        v = value.strip().lower()
        if v in ("", "none", "once", "never", "no"):
            return None
        if v.startswith("monthly"):
            nums = sorted({int(n) for n in re.findall(r"\d+", v[7:])})
            if any(not 1 <= n <= 31 for n in nums) or (not nums and v[7:].strip(" :")):
                raise ValueError(f"invalid repeat '{value}' (monthly, or monthly:15)")
            if len(nums) > 1:
                raise ValueError(f"'{value}': a monthly reminder can only repeat on one day of the month "
                                 "(Alexa keeps just the first); create one reminder per day")
            return cls("MONTHLY", tuple(str(n) for n in nums))
        if v in ("daily", "everyday", "every day"):
            return cls("DAILY")
        if v == "weekdays":
            return cls("WEEKLY", DAYS[:5])
        if v == "weekends":
            return cls("WEEKLY", DAYS[5:])
        days = set()
        for part in v.replace(" ", ",").split(","):
            if not part:
                continue
            if part not in _DAY_NAMES:
                raise ValueError(f"invalid repeat '{value}' (daily, weekdays, weekends, days like mon,wed,fri, "
                                 "or monthly / monthly:15)")
            days.add(_DAY_NAMES[part])
        if len(days) == 7:
            return cls("DAILY")
        return cls("WEEKLY", tuple(d for d in DAYS if d in days))

    @property
    def editable(self) -> bool:
        """Can this library write this rule? (Alarms additionally refuse MONTHLY.)"""
        return self.freq in ("DAILY", "WEEKLY", "MONTHLY")

    def with_day_of(self, when: datetime.datetime) -> "Recurrence":
        """A bare 'monthly' becomes monthly on the day of `when`."""
        if self.freq == "MONTHLY" and not self.days:
            return Recurrence("MONTHLY", (str(when.day),))
        return self

    def describe(self) -> str:
        if self.freq == "DAILY":
            return "daily"
        if self.freq == "MONTHLY":
            return "monthly on " + ",".join(_ordinal(int(d)) for d in self.days) if self.days else "monthly"
        if not self.editable:
            return self.freq.lower()
        if self.days == DAYS[:5]:
            return "weekdays"
        if self.days == DAYS[5:]:
            return "weekends"
        return ",".join(_DAY_LABEL[d] for d in self.days)

    def matches(self, date: datetime.date) -> bool:
        if self.freq == "MONTHLY":
            return not self.days or str(date.day) in self.days
        return self.freq == "DAILY" or DAYS[date.weekday()] in self.days

    def first_occurrence(self, t: datetime.time, now: "datetime.datetime | None" = None) -> datetime.datetime:
        """The first datetime at time `t`, on a matching day, after `now`."""
        now = now or datetime.datetime.now()
        candidate = datetime.datetime.combine(now.date(), t.replace(microsecond=0))
        for _ in range(370 if self.freq == "MONTHLY" else 8):
            if candidate > now and self.matches(candidate.date()):
                return candidate
            candidate += datetime.timedelta(days=1)
        raise ValueError("recurrence matches no day")  # pragma: no cover

    # alarms API: {"freq":"WEEKLY","byDay":["MO"],"interval":1}
    def to_alarm(self) -> dict:
        if self.freq not in ("DAILY", "WEEKLY"):
            raise ValueError(f"alarms can't repeat {self.describe()} (only daily or on days of the week)")
        return {"freq": self.freq, "byDay": list(self.days) if self.freq == "WEEKLY" else [], "interval": 1}

    @classmethod
    def from_alarm(cls, data: "dict | None") -> "Recurrence | None":
        if not data or not data.get("freq"):
            return None
        if data["freq"] == "DAILY":
            return cls("DAILY")
        return cls.parse(",".join(data.get("byDay") or [])) if data.get("byDay") else cls(data["freq"])

    # notifications API: rRuleData. Daily is sent as weekly-on-every-day.
    def to_rrule(self, t: datetime.time, start: datetime.date) -> dict:
        if not self.editable:
            raise ValueError(f"{self.describe()} repeats can't be set from here; change them in the Alexa app")
        if self.freq == "MONTHLY":
            if len(self.days) != 1:
                raise ValueError("a monthly repeat needs exactly one day of the month")
            # Mirrors how Amazon stores monthly reminders (EVERY_X_MONTHS + byMonthDays)
            return {
                "notificationTimes": [_hms(t)],
                "nextTriggerTimes": [],
                "recurStartDate": start.isoformat(),
                "recurStartTime": "00:00:00.000",
                "flexibleRecurringPatternType": "EVERY_X_MONTHS",
                "frequency": "MONTHLY",
                "byMonthDays": [int(self.days[0])],
                "byWeekDays": [],
                "intervals": [1],
            }
        return {
            "notificationTimes": [_hms(t)],
            "nextTriggerTimes": [],
            "recurStartDate": start.isoformat(),
            "recurStartTime": "00:00:00.000",
            "flexibleRecurringPatternType": "EVERY_X_WEEKS",
            "frequency": "WEEKLY",
            "byWeekDays": list(DAYS if self.freq == "DAILY" else self.days),
            "intervals": [1],
        }

    @classmethod
    def from_rrule(cls, data: "dict | None") -> "Recurrence | None":
        """Read rRuleData. Seen from the app: ONCE_A_DAY, EVERY_X_WEEKS (+byWeekDays), EVERY_X_MONTHS (+byMonthDays)."""
        data = data or {}
        pattern = data.get("flexibleRecurringPatternType") or ""
        rules = " ".join(data.get("recurrenceRules") or [])
        if days := data.get("byWeekDays"):
            return cls.parse(",".join(days))
        if pattern == "ONCE_A_DAY" or "FREQ=DAILY" in rules:
            return cls("DAILY")
        if pattern == "EVERY_X_MONTHS" or "FREQ=MONTHLY" in rules:
            return cls("MONTHLY", tuple(str(d) for d in data.get("byMonthDays") or []))
        if pattern or rules:
            return cls(pattern or rules.split(";")[0].removeprefix("FREQ="))
        return None


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def describe(rec: "Recurrence | None") -> str:
    return rec.describe() if rec else ""


def _hms(t: datetime.time) -> str:
    return t.strftime("%H:%M:%S.000")


def _iso_date(s: "str | None") -> "datetime.date | None":
    try:
        return datetime.date.fromisoformat(s[:10]) if s else None
    except ValueError:
        return None


def _parse_time(s: "str | None") -> "datetime.time | None":
    if not s:
        return None
    try:
        return datetime.time.fromisoformat(s[:8])
    except ValueError:
        return None


class Device:
    def __init__(self, raw: dict) -> None:
        self.raw = raw

    serial = property(lambda self: self.raw.get("serialNumber", ""))
    type = property(lambda self: self.raw.get("deviceType", ""))
    name = property(lambda self: self.raw.get("accountName") or self.serial)
    family = property(lambda self: self.raw.get("deviceFamily"))
    online = property(lambda self: bool(self.raw.get("online")))
    customerId = property(lambda self: self.raw.get("deviceOwnerCustomerId"))
    capabilities = property(lambda self: set(self.raw.get("capabilities") or []))

    @property
    def endpointId(self) -> str:
        return f"{self.serial}@{self.type}"

    @property
    def supportsAlarms(self) -> bool:
        return "TIMERS_AND_ALARMS" in self.capabilities

    supportsTimers = supportsAlarms

    @property
    def supportsReminders(self) -> bool:
        return "REMINDERS" in self.capabilities

    def __repr__(self) -> str:
        return f"Device({self.name!r}, {self.serial})"


class Sound:
    def __init__(self, raw: dict) -> None:
        self.raw = raw

    id = property(lambda self: self.raw.get("id") or self.raw.get("assetId", ""))
    name = property(lambda self: self.raw.get("displayName") or self.id)
    folder = property(lambda self: self.raw.get("folder"))
    sampleUrl = property(lambda self: self.raw.get("sampleUrl"))

    @property
    def isSkill(self) -> bool:
        return self.id.startswith("SKILL-") or self.raw.get("providerId") not in (None, "ECHO")

    @property
    def isPaid(self) -> bool:
        return bool(self.raw.get("pricing"))

    def asset(self) -> dict:
        return {"assetId": self.id, "type": "TONE"}

    def __repr__(self) -> str:
        return f"Sound({self.name!r}, {self.id})"


class Item:
    """Common bits of alarms, reminders and timers."""

    kind: Kind

    def __init__(self, raw: dict, device: "Device | None" = None) -> None:
        self.raw = raw
        self.device = device

    @property
    def id(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    @property
    def shortId(self) -> str:
        return self.id[:8]

    @property
    def deviceName(self) -> str:
        if self.device and self.device.name:
            return self.device.name
        if self.raw.get("deviceName") == "OTHER_MOBILE_DEVICE":  # made in the Alexa phone app, rings there
            return "Alexa app (phone)"
        return self.serial or "?"

    serial: str = ""
    label: "str | None" = None
    recurrence: "Recurrence | None" = None
    time: "datetime.datetime | None" = None
    status: str = ""
    enabled: bool = True

    def when_str(self) -> str:
        if self.time is None:
            return "?"
        if self.recurrence:
            return self.time.strftime("%H:%M")
        return self.time.strftime("%a %Y-%m-%d %H:%M")

    def label_str(self) -> str:
        return f'"{self.label}"' if self.label else ""

    def __str__(self) -> str:
        parts = [self.kind.value, self.shortId, self.when_str()]
        if self.recurrence:
            parts.append(f"({self.recurrence.describe()})")
        if self.label_str():
            parts.append(self.label_str())
        parts.append(f"on {self.deviceName}")
        if not self.enabled:
            parts.append("[off]")
        return " ".join(parts)


class Alarm(Item):
    kind = Kind.ALARM

    id = property(lambda self: self.raw.get("alarmToken", ""))
    status = property(lambda self: self.raw.get("status", ""))
    enabled = property(lambda self: self.status == "ON")
    trigger = property(lambda self: self.raw.get("trigger") or {})
    recurrence = property(lambda self: Recurrence.from_alarm(self.trigger.get("recurrence")))
    assets = property(lambda self: self.raw.get("assets") or [])

    @property
    def serial(self) -> str:
        ids = self.raw.get("endpointIds") or [""]
        return ids[0].split("@")[0]

    @property
    def time(self) -> "datetime.datetime | None":
        st = self.trigger.get("scheduledTime")
        if st:
            try:
                return datetime.datetime.fromisoformat(st[:19])
            except ValueError:
                pass
        d, t = self.trigger.get("date"), _parse_time(self.trigger.get("time") or self.trigger.get("timeOfDay"))
        if d and t:
            return datetime.datetime.combine(datetime.date.fromisoformat(d), t)
        return None

    @property
    def sound(self) -> "Sound | None":
        return Sound(self.assets[0]) if self.assets else None

    def label_str(self) -> str:
        return f"[{self.sound.name}]" if self.sound else ""

    def scheduledTime(self) -> str:
        t = self.time
        return t.strftime("%Y-%m-%dT%H:%M:%S") if t else self.trigger.get("scheduledTime", "")


class Reminder(Item):
    kind = Kind.REMINDER

    id = property(lambda self: self.raw.get("id", ""))
    notificationIndex = property(lambda self: self.raw.get("notificationIndex") or self.id)
    shortId = property(lambda self: self.notificationIndex[:8])
    serial = property(lambda self: self.raw.get("deviceSerialNumber", ""))
    label = property(lambda self: self.raw.get("reminderLabel"))
    text = label
    enabled = property(lambda self: self.raw.get("status", "") in ("ON", "SNOOZED", ""))  # snoozed = rings again

    @property
    def status(self) -> str:
        """ON, or DONE for a one-off that has already rung (Amazon reports it as OFF and keeps it for a while)."""
        s = self.raw.get("status", "")
        if s == "OFF" and self.raw.get("lastTriggerTimeInUtc") and not self.recurrence:
            return "DONE"
        return s

    recurrence = property(lambda self: Recurrence.from_rrule(self.raw.get("rRuleData")))
    note = property(lambda self: (self.raw.get("taskMetadata") or {}).get("note") or None)
    important = property(lambda self: bool((self.raw.get("taskMetadata") or {}).get("flagged")))
    keepReminding = property(lambda self: bool((self.raw.get("followUpMetadata") or {}).get("requiresFollowUp")))

    def label_str(self) -> str:
        parts = [f'"{self.label}"'] if self.label else []
        if self.important:
            parts.append("[important]")
        if self.keepReminding:
            parts.append("[keep reminding]")
        if self.note:
            parts.append(f"(note: {self.note})")
        return " ".join(parts)

    @property
    def time(self) -> "datetime.datetime | None":
        """Device-local time. Repeating: next occurrence at the rule's time; one-off: originalDate/Time.

        alarmTime is a last resort: for repeating reminders it's midnight UTC of the next day, not the ring time.
        """
        rrule = self.raw.get("rRuleData") or {}
        times = rrule.get("notificationTimes") or []
        rule_t = _parse_time(times[0]) if times else None
        if rule_t:
            nxt = (rrule.get("nextTriggerTimes") or [None])[0]
            day = _iso_date(nxt) or _iso_date(self.raw.get("originalDate")) or datetime.date.today()
            return datetime.datetime.combine(day, rule_t)
        d, t = _iso_date(self.raw.get("originalDate")), _parse_time(self.raw.get("originalTime"))
        if d and t:
            return datetime.datetime.combine(d, t)
        ms = self.raw.get("alarmTime")
        return datetime.datetime.fromtimestamp(ms / 1000) if ms else None


class Task(Item):
    """An Alexa to-do item. Amazon returns these from the reminders endpoint with type "Task".

    Read-only here: they can be listed, but not created, edited or deleted.
    """
    kind = Kind.TASK

    id = property(lambda self: self.raw.get("id", ""))
    notificationIndex = property(lambda self: self.raw.get("notificationIndex") or self.id)
    shortId = property(lambda self: self.notificationIndex[:8])
    serial = property(lambda self: self.raw.get("deviceSerialNumber", ""))
    label = property(lambda self: self.raw.get("reminderLabel"))
    text = label
    done = property(lambda self: self.raw.get("status") == "OFF")  # done items come back OFF (confirmed)
    enabled = property(lambda self: not self.done)
    status = property(lambda self: "DONE" if self.done else "TO DO")
    note = property(lambda self: (self.raw.get("taskMetadata") or {}).get("note") or None)
    important = property(lambda self: bool((self.raw.get("taskMetadata") or {}).get("flagged")))
    deviceName = property(lambda self: "")

    @property
    def time(self) -> "datetime.datetime | None":
        """The due date (and time, if set), when the item has one."""
        meta = self.raw.get("taskMetadata") or {}
        due = meta.get("dueDate")
        day = _iso_date(due) if isinstance(due, str) else None
        if day is None:
            return None
        t = _parse_time(meta.get("dueTime")) if isinstance(meta.get("dueTime"), str) else None
        return datetime.datetime.combine(day, t or datetime.time(0))

    def when_str(self) -> str:
        t = self.time
        if t is None:
            return ""
        due = (self.raw.get("taskMetadata") or {}).get("dueTime")
        return f"due {t:%a %Y-%m-%d %H:%M}" if due else f"due {t:%a %Y-%m-%d}"

    def label_str(self) -> str:
        parts = [f'"{self.label}"'] if self.label else []
        if self.important:
            parts.append("[important]")
        if self.note:
            parts.append(f"(note: {self.note})")
        return " ".join(parts)

    def __str__(self) -> str:
        parts = ["task", self.shortId, self.when_str(), self.label_str(), "[done]" if self.done else ""]
        return " ".join(p for p in parts if p)


class Timer(Item):
    kind = Kind.TIMER

    id = property(lambda self: self.raw.get("id", ""))
    notificationIndex = property(lambda self: self.raw.get("notificationIndex") or self.id)
    shortId = property(lambda self: self.notificationIndex[:8])
    serial = property(lambda self: self.raw.get("deviceSerialNumber", ""))
    label = property(lambda self: self.raw.get("timerLabel"))
    status = property(lambda self: self.raw.get("status", ""))
    enabled = property(lambda self: self.status in ("ON", "PAUSED"))  # running or paused = still exists
    paused = property(lambda self: self.status == "PAUSED")
    duration = property(lambda self: datetime.timedelta(milliseconds=self.raw.get("originalDurationInMillis") or 0))
    remaining = property(lambda self: datetime.timedelta(milliseconds=self.raw.get("remainingTime") or 0))

    @property
    def time(self) -> "datetime.datetime | None":
        ms = self.raw.get("triggerTime")
        return datetime.datetime.fromtimestamp(ms / 1000) if ms else None

    def when_str(self) -> str:
        rem = int(self.remaining.total_seconds())
        left = f"{_clock(rem)} left" if rem else "?"
        if self.status == "PAUSED":
            return f"{left} (paused)"
        return f"{left}, rings {self.time:%H:%M:%S}" if self.time else left

    def label_str(self) -> str:
        total = f"{_clock(int(self.duration.total_seconds()))} total"
        return f'"{self.label}" {total}' if self.label else total


def _clock(seconds: int) -> str:
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


_TIMER_RESPONSE_ONLY = ("deviceName", "mrmGroupContext", "rRuleData")


def _timer_put_body(timer: "Timer", **changes) -> dict:
    """The app PUTs the whole timer back with a few fields changed (captured pause/resume)."""
    body = {k: v for k, v in copy.deepcopy(timer.raw).items() if k not in _TIMER_RESPONSE_ONLY}
    body.update(mobileDsn=None, useRESTApi=False, **changes)
    return body


def timer_pause_body(timer: "Timer", now_ms: "int | None" = None) -> dict:
    """status PAUSED; alarmTime = when it would have rung, triggerTime 0, remainingTime frozen."""
    now_ms = int(_time.time() * 1000) if now_ms is None else now_ms
    trigger = timer.raw.get("triggerTime") or 0
    remaining = max(trigger - now_ms, 0) if trigger else timer.raw.get("remainingTime") or 0
    return _timer_put_body(timer, status="PAUSED", alarmTime=trigger, triggerTime=0, remainingTime=remaining)


def timer_resume_body(timer: "Timer") -> dict:
    """status ON, alarmTime 0; the server sets triggerTime = now + remainingTime."""
    return _timer_put_body(timer, status="ON", alarmTime=0, triggerTime=0)


def timer_adjust_body(timer: "Timer", delta: datetime.timedelta, now_ms: "int | None" = None) -> dict:
    """A paused timer with `delta` added to its time left (negative subtracts)."""
    if timer.paused:
        body = _timer_put_body(timer, remainingTime=timer.raw.get("remainingTime") or 0)
    else:
        body = timer_pause_body(timer, now_ms)
    left = body["remainingTime"]
    body["remainingTime"] = left + int(delta.total_seconds() * 1000)
    if body["remainingTime"] < 1000:
        raise ValueError(f"the timer only has {_clock(left // 1000)} left; delete it instead")
    return body


def alarm_create_body(device: Device, when: datetime.datetime, recurrence: "Recurrence | None", sound: Sound) -> dict:
    trigger = {"scheduledTime": when.strftime("%Y-%m-%dT%H:%M:%S")}
    if recurrence:
        trigger["recurrence"] = recurrence.to_alarm()
    return {
        "trigger": trigger,
        "endpointId": device.endpointId,
        "assets": [sound.asset()],
        "extensions": [{"name": "WAKE_UP_LIGHTS", "enablement": "DISABLED"}],
    }


def alarm_update_body(when: datetime.datetime, recurrence: "Recurrence | None", assets: list[dict]) -> dict:
    trigger = {"scheduledTime": when.strftime("%Y-%m-%dT%H:%M:%S")}
    if recurrence:
        trigger["recurrence"] = recurrence.to_alarm()
    return {"trigger": trigger, "assets": [{"assetId": a["assetId"], "type": a.get("type", "TONE")} for a in assets],
            "extensions": []}


def alarm_activate_body(alarm: Alarm) -> dict:
    return {
        "trigger": {"scheduledTime": alarm.scheduledTime()},
        "assets": [{"assetId": a["assetId"], "type": a.get("type", "TONE")} for a in alarm.assets],
    }


def reminder_body(
    device: Device,
    when: datetime.datetime,
    text: str,
    recurrence: "Recurrence | None",
    person_id: str,
    today: "datetime.date | None" = None,
    note: "str | None" = None,
    important: bool = False,
    keep_reminding: bool = False,
) -> dict:
    """A new reminder, shaped like the app's PUT /api/notifications/null body."""
    if recurrence:
        recurrence = recurrence.with_day_of(when)
        rrule, date, time, alarm_ms = recurrence.to_rrule(when.time(), today or datetime.date.today()), None, None, 0
    else:
        rrule, date, time = None, when.date().isoformat(), _hms(when.time())
        alarm_ms = int(when.timestamp() * 1000)
    return {
        "alarmTime": alarm_ms, "createdDate": 0, "lastUpdatedDate": 0,
        "deviceSerialNumber": device.serial, "deviceType": device.type,
        "id": "null", "notificationIndex": None,
        "originalDate": date, "originalTime": time, "recurringPattern": None,
        "remainingTime": 0, "reminderLabel": text, "status": "ON", "timeZoneId": None,
        "triggerTime": 0, "type": "Reminder", "version": None, "mobileDsn": None,
        "rRuleData": rrule,
        "reminderAppLabel": None, "lastOccurrenceTimeInMilli": 0, "recurrenceEligibility": False,
        "deliveryType": "SPOKEN_DEVICE", "loopCount": None,
        "followUpMetadata": {"requiresFollowUp": bool(keep_reminding), "followUpStatus": "UNACKNOWLEDGED"},
        "deliveryMetadata": None, "mrmGroupContext": {"groupId": "", "groupType": ""},
        "originalDurationInMillis": 0,
        "taskMetadata": {"note": note or None, "dueDate": None, "dueTime": None, "source": None,
                         "flagged": bool(important), "overdueAt": None},
        "personProfile": {"firstName": None, "lastName": None, "speakerId": None, "personId": person_id,
                          "childData": None, "isVpcGranted": None},
        "assigner": {"person": {"id": person_id}},
        "clientType": "VOX",
    }


def spoken_duration(duration: datetime.timedelta) -> str:
    """'1 hour and 30 minutes'."""
    total = int(abs(duration.total_seconds()))
    if total <= 0:
        raise ValueError("duration must be at least a second")
    h, m, s = total // 3600, total % 3600 // 60, total % 60
    parts = [f"{n} {unit}{'' if n == 1 else 's'}" for n, unit in ((h, "hour"), (m, "minute"), (s, "second")) if n]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def timer_phrase(duration: datetime.timedelta, label: "str | None" = None) -> str:
    """'set a timer for 1 hour and 30 minutes' / 'set a pizza timer for 10 minutes'."""
    if duration.total_seconds() <= 0:
        raise ValueError("timer duration must be positive")
    spoken = spoken_duration(duration)
    return f"set a {label} timer for {spoken}" if label else f"set a timer for {spoken}"
