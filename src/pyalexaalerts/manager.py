import copy
import datetime
import logging
import time as _time
from collections.abc import Callable, Iterator
from pathlib import Path

from .api import AlexaAPI
from .exception import DeviceNotFoundException, NotificationNotFoundException
from .resource import (
    Alarm,
    Device,
    Item,
    Kind,
    Recurrence,
    Reminder,
    Sound,
    Task,
    Timer,
    alarm_activate_body,
    alarm_create_body,
    alarm_update_body,
    reminder_body,
    timer_adjust_body,
    timer_pause_body,
    timer_phrase,
    timer_resume_body,
)

logger = logging.getLogger(__name__)

_UNSET = object()


def next_occurrence(t: datetime.time, now: "datetime.datetime | None" = None) -> datetime.datetime:
    """The next local datetime at time-of-day `t` (today if still ahead, else tomorrow)."""
    return Recurrence("DAILY").first_occurrence(t, now)


def resolve_when(when, recurrence: "Recurrence | None" = None, now: "datetime.datetime | None" = None) -> datetime.datetime:
    """datetime → itself; timedelta → now + delta; time → next occurrence (on a matching day if recurring)."""
    now = now or datetime.datetime.now()
    if isinstance(when, datetime.timedelta):
        return (now + when).replace(microsecond=0)
    if isinstance(when, datetime.datetime):
        return when.replace(microsecond=0)
    if isinstance(when, datetime.time):
        return (recurrence or Recurrence("DAILY")).first_occurrence(when, now)
    raise TypeError(f"Expected datetime, time or timedelta, got {type(when).__name__}")


def _not_task(item: Item) -> Item:
    if isinstance(item, Task):
        raise ValueError(f"{item} is an Alexa to-do item; those can only be listed here")
    return item


def _clean_note(note: "str | None") -> "str | None":
    return note.strip() or None if note else None


class AlexaAlerts:
    """High-level manager for Alexa alarms, reminders and timers.

    Example:
        alexa = AlexaAlerts()
        alexa.resync()
        alarm = alexa.createAlarm("Bedroom", datetime.time(6, 45), recurrence="weekdays")
        alexa.disable(alarm)                  # switch off, keep it
        alexa.enable(alarm)
        alexa.delete(alarm)
    """

    def __init__(
        self,
        amazon_domain: str = "amazon.com",
        service_auth_path: "Path | str | None" = None,
        runtime_creds_path: "Path | str | None" = None,
        alexa_host: "str | None" = None,
        person_id: "str | None" = None,
        api: "AlexaAPI | None" = None,
    ) -> None:
        self.api = api or AlexaAPI(
            amazon_domain=amazon_domain,
            service_auth_path=service_auth_path,
            runtime_creds_path=runtime_creds_path,
            alexa_host=alexa_host,
            person_id=person_id,
        )
        self._devices: dict[str, Device] = {}
        self._items: dict[Kind, dict[str, Item]] = {k: {} for k in Kind}
        self._sounds: dict[tuple[str, str], list[Sound]] = {}

    def resync(self, kinds: "set[Kind] | None" = None) -> None:
        """Fetch devices, then alarms/reminders/timers (or just `kinds`), replacing local state."""
        self._devices = {d["serialNumber"]: Device(d) for d in self.api.getDevices() if d.get("serialNumber")}
        self.refresh(kinds)

    def refresh(self, kinds: "set[Kind] | None" = None) -> None:
        reminders_reply = None  # one request returns both reminders and to-do items (tasks)
        for kind in set(Kind) if kinds is None else kinds:
            if kind is Kind.ALARM:
                raws, cls = self.api.getAlarms(), Alarm
            elif kind in (Kind.REMINDER, Kind.TASK):
                if reminders_reply is None:
                    reminders_reply = self.api.getNotifications("REMINDER")
                ntype, cls = ("Reminder", Reminder) if kind is Kind.REMINDER else ("Task", Task)
                raws = [r for r in reminders_reply if r.get("type") == ntype]
            else:
                raws, cls = [r for r in self.api.getNotifications("TIMER") if r.get("type") == "Timer"], Timer
            self._items[kind] = {}
            for raw in raws:
                self._add(cls(raw))

    def _add(self, item: Item) -> Item:
        item.device = self._devices.get(item.serial)
        self._items[item.kind][item.id] = item
        return item

    def devices(self, alarms: "bool | None" = None, reminders: "bool | None" = None) -> list[Device]:
        return [
            d for d in self._devices.values()
            if (alarms is None or d.supportsAlarms == alarms) and (reminders is None or d.supportsReminders == reminders)
        ]

    def getDevice(self, ref: "str | Device") -> Device:
        """Find a device by serial number or name (exact, case-insensitive; else unique substring)."""
        if isinstance(ref, Device):
            return ref
        if ref in self._devices:
            return self._devices[ref]
        key = ref.casefold()
        exact = [d for d in self._devices.values() if d.name.casefold() == key]
        matches = exact or [d for d in self._devices.values() if key in d.name.casefold()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise DeviceNotFoundException(f"No device matches '{ref}'")
        raise DeviceNotFoundException(f"'{ref}' matches several devices: {', '.join(sorted(d.name for d in matches))}")

    def sounds(self, device: "str | Device", alert_type: str = "Alarm") -> list[Sound]:
        """Sounds offered for `device`; alert_type "Alarm" or "Timer" (cached per device and type)."""
        dev = self.getDevice(device)
        key = (dev.serial, alert_type)
        if key not in self._sounds:
            self._sounds[key] = [Sound(s) for s in self.api.getSounds(dev.serial, dev.type, alert_type)]
        return self._sounds[key]

    def defaultSound(self, device: "str | Device", alert_type: str = "Alarm") -> "Sound | None":
        """The device's current default sound for alarms or timers ({"defaultNotificationSound": {...}})."""
        dev = self.getDevice(device)
        raw = self.api.getDefaultSound(dev.serial, dev.type, alert_type) or {}
        raw = raw.get("defaultNotificationSound") or raw
        return Sound(raw) if raw.get("id") else None

    def getSound(self, device: "str | Device", ref: "str | Sound | None" = None) -> Sound:
        """A built-in sound by id or name (exact, else unique substring); the device default when `ref` is None."""
        if isinstance(ref, Sound):
            return ref
        dev = self.getDevice(device)
        if ref is None:
            return self.defaultSound(dev) or Sound({"id": "system_alerts_melodic_01", "displayName": "Simple Alarm"})
        sounds = self.sounds(dev)
        key = ref.casefold()
        matches = [s for s in sounds if s.id.casefold() == key or s.name.casefold() == key] or \
                  [s for s in sounds if key in s.name.casefold() or key in s.id.casefold()]
        if not matches and ref.startswith("system_"):
            return Sound({"id": ref, "displayName": ref})
        if not matches and any(key in (s.name.casefold(), s.id.casefold()) for s in self.sounds(dev, "Timer")):
            raise ValueError(f"'{ref}' is a timer sound, not an alarm sound (see `sounds -d {dev.name}`)")
        if len(matches) != 1:
            names = ", ".join(sorted(s.name for s in matches)) if matches else "none (see the `sounds` command)"
            raise ValueError(f"Sound '{ref}' matches {len(matches)} sounds: {names}")
        if matches[0].isSkill:
            raise ValueError(f"'{matches[0].name}' is a skill sound; only built-in sounds are supported")
        return matches[0]

    def all(self) -> list[Item]:
        return [i for items in self._items.values() for i in items.values()]

    def find(
        self,
        kind: "Kind | str | None" = None,
        device: "str | Device | None" = None,
        enabled: "bool | None" = None,
        query: "str | None" = None,
        func: "Callable[[Item], bool] | None" = None,
    ) -> Iterator[Item]:
        # to-do items (tasks) only when asked for by kind
        kinds = [Kind.from_str(kind)] if kind is not None else [k for k in Kind if k is not Kind.TASK]
        serial = self.getDevice(device).serial if device is not None else None
        return (
            i for k in kinds for i in self._items[k].values()
            if (serial is None or i.serial == serial)
            and (enabled is None or i.enabled == enabled)
            and (query is None or query.casefold() in (i.label or "").casefold())
            and (func is None or func(i))
        )

    def get(self, ref: "str | Item", kind: "Kind | str | None" = None) -> Item:
        """Find an item by full id, or a unique prefix of the short id shown by `list`."""
        if isinstance(ref, Item):
            return ref
        pool = list(self.find(kind))
        exact = [i for i in pool if ref in (i.id, getattr(i, "notificationIndex", None))]
        matches = exact or [i for i in pool if i.shortId.startswith(ref) or i.id.startswith(ref)
                            or getattr(i, "notificationIndex", "").startswith(ref)]
        if len(matches) == 1:
            return matches[0]
        what = Kind.from_str(kind).value if kind else "alarm/reminder/timer"
        if not matches:
            if kind is None and any(t.shortId.startswith(ref) or t.id.startswith(ref)
                                    for t in self._items[Kind.TASK].values()):
                raise ValueError(f"'{ref}' is an Alexa to-do item; those can only be listed here")
            raise NotificationNotFoundException(f"No {what} matches '{ref}'")
        raise NotificationNotFoundException(f"'{ref}' is ambiguous ({len(matches)} matches); use a longer id")

    def createAlarm(
        self,
        device: "str | Device",
        when,
        recurrence: "str | Recurrence | None" = None,
        sound: "str | Sound | None" = None,
        enabled: bool = True,
    ) -> Alarm:
        """Create an alarm.

        Args:
            device: Device name/serial or Device.
            when: datetime (device-local), time of day (next matching occurrence), or timedelta from now.
            recurrence: 'daily', 'weekdays', 'weekends', 'mon,wed,fri', ... (one alarm covers all the days).
            sound: Built-in sound name/id (see sounds()); the device default when omitted.
            enabled: Create it switched off when False.
        """
        dev = self.getDevice(device)
        rec = Recurrence.parse(recurrence)
        body = alarm_create_body(dev, resolve_when(when, rec), rec, self.getSound(dev, sound))
        raw = self.api.createAlarm(body)
        if not raw or "alarmToken" not in raw:
            raise RuntimeError("Alarm created but the server returned no alarm; run `list` to check")
        alarm = self._add(Alarm(raw))
        return self.disable(alarm) if not enabled else alarm

    def _switchable(self, ref) -> "Alarm | Timer":
        item = _not_task(self.get(ref) if not isinstance(ref, Item) else ref)
        if isinstance(item, Reminder):
            raise ValueError("reminders have no on/off switch; delete it instead")
        return item

    def enable(self, ref: "str | Alarm | Timer") -> "Alarm | Timer":
        """Switch an alarm on (a past date moves to the next occurrence), or resume a paused timer."""
        alarm = self._switchable(ref)
        if isinstance(alarm, Timer):
            return self.resumeTimer(alarm)
        raw = self.api.activateAlarm(alarm.id, alarm_activate_body(alarm))
        return self._replace(alarm, Alarm(raw) if raw and "alarmToken" in raw else _with(alarm, status="ON"))

    def disable(self, ref: "str | Alarm") -> Alarm:
        """Switch an alarm off without deleting it (enable() switches it back on).

        Doesn't silence an alarm that is ringing right now. Timers have pause() instead.
        """
        alarm = self._switchable(ref)
        if isinstance(alarm, Timer):
            raise ValueError("timers have no off switch; use pause (resume later) or delete")
        raw = self.api.cancelAlarm(alarm.id)  # the server calls switching off "cancel"
        return self._replace(alarm, Alarm(raw) if raw and "alarmToken" in raw else _with(alarm, status="OFF"))

    start = enable

    def updateAlarm(self, ref, when=None, recurrence=_UNSET, sound: "str | Sound | None" = None) -> Alarm:
        alarm = self.get(ref, Kind.ALARM) if not isinstance(ref, Item) else ref
        if not isinstance(alarm, Alarm):
            raise ValueError(f"not an alarm: {alarm}")
        rec = alarm.recurrence if recurrence is _UNSET else Recurrence.parse(recurrence)
        if when is None:
            t = alarm.time.time() if alarm.time else None
            if t is None:
                raise ValueError("alarm has no time; pass when=")
            # Changing only the repeat days: move to the first matching day at the same time.
            when_dt = rec.first_occurrence(t) if rec and recurrence is not _UNSET else alarm.time
        else:
            when_dt = resolve_when(when, rec)
        assets = [self.getSound(alarm.device or alarm.serial, sound).asset()] if sound else alarm.assets
        raw = self.api.updateAlarm(alarm.id, alarm_update_body(when_dt, rec, assets))
        return self._replace(alarm, Alarm(raw) if raw and "alarmToken" in raw else alarm)

    def createReminder(self, device: "str | Device", when, text: str, recurrence: "str | Recurrence | None" = None,
                       note: "str | None" = None, important: bool = False, keep_reminding: bool = False) -> Reminder:
        """Create a reminder that Alexa will announce with `text`.

        note: extra text stored with the reminder; important: flag it; keep_reminding: Alexa's follow-up
        setting (requiresFollowUp).
        """
        if not text or not text.strip():
            raise ValueError("Reminder text is required")
        dev = self.getDevice(device)
        rec = Recurrence.parse(recurrence)
        before = set(self._items[Kind.REMINDER])
        body = reminder_body(dev, resolve_when(when, rec), text.strip(), rec, self.api.personId,
                             note=_clean_note(note), important=important, keep_reminding=keep_reminding)
        raw = self.api.createReminder(body)
        if raw and raw.get("id") and raw["id"] != "null":
            return self._add(Reminder(raw))
        self.refresh({Kind.REMINDER})
        new = [r for k, r in self._items[Kind.REMINDER].items() if k not in before and r.serial == dev.serial]
        if len(new) != 1:
            raise RuntimeError("Reminder created but could not be identified; run `list` to check")
        return new[0]

    def updateReminder(self, ref, when=None, text: "str | None" = None, recurrence=_UNSET, note=_UNSET,
                       important: "bool | None" = None, keep_reminding: "bool | None" = None) -> Reminder:
        """Change a reminder's time, text, repeat, note or flags (for a repeating one, the whole series).

        note=None or "" removes the note; important / keep_reminding = True/False switch those on/off.
        """
        rem = self.get(ref, Kind.REMINDER)
        body = {**copy.deepcopy(rem.raw), "clientType": "VOX", "status": "ON"}
        if text is not None:
            if not text.strip():
                raise ValueError("Reminder text can't be empty")
            body["reminderLabel"] = text.strip()
        if note is not _UNSET or important is not None:
            task = dict(body.get("taskMetadata") or {})
            if note is not _UNSET:
                task["note"] = _clean_note(note)
            if important is not None:
                task["flagged"] = bool(important)
            body["taskMetadata"] = task
        if keep_reminding is not None:
            body["followUpMetadata"] = {"followUpStatus": "UNACKNOWLEDGED",
                                        **(body.get("followUpMetadata") or {}), "requiresFollowUp": bool(keep_reminding)}
        rec = rem.recurrence if recurrence is _UNSET else Recurrence.parse(recurrence)
        if rec and not rec.editable:
            raise ValueError(f"this reminder repeats {rec.describe()}; edit it in the Alexa app, or pass a new "
                             "repeat (daily, weekdays, mon,wed, monthly:15, ...) to replace it")
        # Sending a repeating reminder back as the server stores it (alarmTime/originalDate set, frequency null,
        # recurrenceRules...) silently turns it into a one-off. So always rebuild its schedule the
        # way creation does, even for a text-only edit.
        if when is not None or recurrence is not _UNSET or rec is not None:
            base = when if when is not None else (rem.time.time() if rem.time else None)
            if base is None:
                raise ValueError("reminder has no time; pass when=")
            new = reminder_body(rem.device or _stub_device(rem), resolve_when(base, rec), body["reminderLabel"], rec,
                                self.api.personId)
            for key in ("alarmTime", "originalDate", "originalTime", "rRuleData"):
                body[key] = new[key]
        raw = self.api.updateReminder(body)
        return self._replace(rem, Reminder(raw) if raw and raw.get("id") else Reminder(body))

    def updateReminderOccurrence(self, ref, when=None, text: "str | None" = None) -> Reminder:
        """Change only the NEXT occurrence of a repeating reminder; the rest of the series is untouched.

        `when`: a time (same day, new time), a date (new day, same time), a datetime, or a timedelta from now.
        Alexa splits the occurrence off as a separate one-off reminder (new id), which is returned. The series'
        next occurrence then moves on, so calling this again changes the one after.
        """
        rem = self.get(ref, Kind.REMINDER) if not isinstance(ref, Item) else ref
        if not isinstance(rem, Reminder):
            raise ValueError(f"not a reminder: {rem}")
        if not rem.recurrence:
            raise ValueError("only repeating reminders have occurrences; edit it without --next-only")
        occurrence = rem.time
        if occurrence is None:
            raise ValueError("can't tell when this reminder's next occurrence is")
        if when is None and text is None:
            raise ValueError("nothing to change; pass a new time/date and/or text")
        if text is not None and not text.strip():
            raise ValueError("Reminder text can't be empty")
        if when is None:
            new_when = occurrence
        elif isinstance(when, datetime.time):
            new_when = datetime.datetime.combine(occurrence.date(), when)
        elif isinstance(when, datetime.date) and not isinstance(when, datetime.datetime):
            new_when = datetime.datetime.combine(when, occurrence.time())
        else:
            new_when = resolve_when(when)
        if new_when <= datetime.datetime.now():
            raise ValueError(f"{new_when:%Y-%m-%d %H:%M} is in the past")

        raw = rem.raw
        body = reminder_body(rem.device or _stub_device(rem), new_when, text.strip() if text else rem.text,
                             None, self.api.personId, note=rem.note, important=rem.important,
                             keep_reminding=rem.keepReminding)
        body.pop("rRuleData")
        body.update(
            id=rem.id, notificationIndex=rem.notificationIndex, status="INSTANCE_UPDATED",
            alarmTime=raw.get("alarmTime"), createdDate=raw.get("createdDate", 0),
            lastUpdatedDate=raw.get("lastUpdatedDate", 0), version=raw.get("version"),
        )
        if raw.get("assigner"):
            body["assigner"] = raw["assigner"]
        body["taskMetadata"]["overdueAt"] = (raw.get("taskMetadata") or {}).get("overdueAt")
        result = self.api.updateReminder(body) or {}
        self.refresh({Kind.REMINDER})
        new_id = result.get("id")
        return self._items[Kind.REMINDER].get(new_id) or Reminder(result if new_id else body, rem.device)

    def createTimer(self, device: "str | Device", duration: datetime.timedelta, label: "str | None" = None,
                    wait: float = 15.0, poll: float = 1.5) -> "Timer | None":
        """Ask `device` to start a timer via a text command, then poll until it shows up.

        Alexa interprets the phrase like speech, so check the returned timer's duration.
        Returns None if no new timer appeared within `wait` seconds.
        """
        dev = self._commandable(device)
        self.refresh({Kind.TIMER})
        before = set(self._items[Kind.TIMER])
        self.api.runTextCommand(dev.serial, dev.type, dev.customerId, timer_phrase(duration, label))
        deadline = _time.monotonic() + wait
        while _time.monotonic() < deadline:
            _time.sleep(poll)
            self.refresh({Kind.TIMER})
            new = [t for k, t in self._items[Kind.TIMER].items() if k not in before and t.serial == dev.serial]
            if new:
                return new[0]
        return None

    def _timer(self, ref) -> Timer:
        item = self.get(ref) if not isinstance(ref, Item) else ref
        if not isinstance(item, Timer):
            raise ValueError(f"{item.kind.value}s can't be paused; only timers can")
        return item

    def pauseTimer(self, ref: "str | Timer") -> Timer:
        timer = self._timer(ref)
        if timer.paused:
            return timer
        body = timer_pause_body(timer)
        raw = self.api.updateTimer(body)
        return self._replace(timer, Timer(raw if raw and raw.get("id") else body))

    def resumeTimer(self, ref: "str | Timer") -> Timer:
        timer = self._timer(ref)
        if not timer.paused:
            return timer
        body = timer_resume_body(timer)
        raw = self.api.updateTimer(body)
        return self._replace(timer, Timer(raw if raw and raw.get("id") else body))

    pause, resume = pauseTimer, resumeTimer

    def adjustTimer(self, ref: "str | Timer", delta: datetime.timedelta) -> Timer:
        """Add time to a timer (negative `delta` subtracts). Silent: no voice command is involved.

        Pauses the timer with the new time left, then resumes it if it was running (a paused timer stays paused).
        """
        timer = self.get(ref) if not isinstance(ref, Item) else ref
        if not isinstance(timer, Timer):
            raise ValueError(f"{timer.kind.value}s have no time to add or subtract; only timers do")
        if abs(delta.total_seconds()) < 1:
            raise ValueError("give an amount of at least a second")
        body = timer_adjust_body(timer, delta)
        raw = self.api.updateTimer(body)
        result = Timer(raw if raw and raw.get("id") else body, timer.device)
        if not timer.paused:
            try:
                raw = self.api.updateTimer(timer_resume_body(result))
            except Exception as e:
                self._replace(timer, result)
                raise RuntimeError(f"the new time was set but the timer is left paused ({e}); "
                                   f"run `resume {timer.shortId}`") from e
            result = Timer(raw if raw and raw.get("id") else timer_resume_body(result), timer.device)
        self._replace(timer, result)
        self.refresh({Kind.TIMER})
        return self._items[Kind.TIMER].get(timer.id) or result

    def _commandable(self, device: "str | Device") -> Device:
        dev = self.getDevice(device)
        if not dev.customerId:
            raise ValueError(f"{dev.name} has no owner customer id; can't send it commands")
        return dev

    def sendCommand(self, device: "str | Device", text: str) -> None:
        """Have `device` act on `text` as if it had been spoken to it. Alexa may answer out loud."""
        dev = self._commandable(device)
        self.api.runTextCommand(dev.serial, dev.type, dev.customerId, text)

    def stopAlert(self, device: "str | Device") -> None:
        """Silence whatever is ringing on `device` by typing "stop", like saying "Alexa, stop"."""
        self.sendCommand(device, "stop")

    def snooze(self, device: "str | Device", duration: "datetime.timedelta | None" = None) -> None:
        """Snooze the alarm or reminder ringing on `device`, like saying "Alexa, snooze" (optionally "for N minutes")."""
        if duration is None:
            return self.sendCommand(device, "snooze")
        minutes = round(duration.total_seconds() / 60)
        if not 1 <= minutes <= 60:
            raise ValueError("snooze for 1 to 60 minutes")
        self.sendCommand(device, f"snooze for {minutes} minute{'s' if minutes != 1 else ''}")

    def update(self, ref, **kwargs) -> Item:
        item = _not_task(self.get(ref))
        if isinstance(item, Alarm):
            return self.updateAlarm(item, **kwargs)
        if isinstance(item, Reminder):
            return self.updateReminder(item, **kwargs)
        raise ValueError("timers can't be edited; delete it and create a new one")

    def delete(self, ref: "str | Item") -> None:
        """Permanently delete an alarm, reminder (whole series) or timer."""
        item = _not_task(self.get(ref))
        if isinstance(item, Alarm):
            self.api.deleteAlarm(item.id)
        elif isinstance(item, Reminder):
            self.api.deleteReminder(item.id)
        elif isinstance(item, Timer):
            self.api.deleteTimer(item.id)
        else:
            raise ValueError(f"can't delete {item}")
        self._items[item.kind].pop(item.id, None)

    cancel = delete  # as in "Alexa, cancel my timer"

    def _replace(self, old: Item, new: Item) -> Item:
        self._items[old.kind].pop(old.id, None)
        return self._add(new)


def _with(alarm: Alarm, **changes) -> Alarm:
    return Alarm({**copy.deepcopy(alarm.raw), **changes})


def _stub_device(item: Item) -> Device:
    return Device({"serialNumber": item.raw.get("deviceSerialNumber"), "deviceType": item.raw.get("deviceType")})
