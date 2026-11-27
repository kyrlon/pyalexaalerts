"""Command-line app for Alexa alarms, reminders and timers.

Examples:
    pyalexaalerts devices
    pyalexaalerts list
    pyalexaalerts sounds -d bedroom
    pyalexaalerts alarm -d bedroom -t 6:45 -r weekdays --sound "Simple Alarm"
    pyalexaalerts alarm -d bedroom -t 7:00 -r mon,wed,sun
    pyalexaalerts reminder -d kitchen --in 20m -m "Take the pizza out"
    pyalexaalerts reminder -d kitchen -t 9pm -r tue,thu -m "Take out trash"
    pyalexaalerts reminder -d kitchen -t 8am -r daily -m "Take medicine" --keep-reminding
    pyalexaalerts timer -d kitchen 10m -l pizza
    pyalexaalerts off 1a2b3c4d           # switch an alarm off (keeps it)
    pyalexaalerts on 1a2b3c4d            # switch it back on
    pyalexaalerts pause 9f8e7d6c         # pause a timer
    pyalexaalerts resume 9f8e7d6c        # resume it
    pyalexaalerts add-time 9f8e7d6c 5m   # add 5 minutes to a timer
    pyalexaalerts subtract-time 9f8e7d6c 2m
    pyalexaalerts stop-alert -d bedroom   # silence what's ringing ("Alexa, stop")
    pyalexaalerts snooze -d bedroom       # snooze a ringing alarm or reminder
    pyalexaalerts edit 1a2b3c4d -t 7:15
    pyalexaalerts delete 1a2b3c4d
"""
import argparse
import datetime
import json
import logging
import re
import sys
from pathlib import Path

from .auth import SUPPORTED_DOMAINS
from .exception import AlexaAPIError, AlexaSessionExpiredException, DeviceNotFoundException, NotificationNotFoundException
from .manager import AlexaAlerts
from .resource import Item, Kind, Recurrence, Timer, describe

_TIME_FORMATS = ("%H:%M", "%H:%M:%S", "%I:%M%p", "%I:%M %p", "%I%p", "%I %p", "%H%M")
_DURATION_RE = re.compile(r"^\s*(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?:in)?)?\s*(?:(\d+)\s*s)?\s*$", re.I)
ALL = None  # resync everything


def parse_time(value: str) -> datetime.time:
    v = value.strip().upper().replace(".", "")
    for fmt in _TIME_FORMATS:
        try:
            return datetime.datetime.strptime(v, fmt).time()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"invalid time '{value}' (try 07:30, 7:30pm, 19:30)")


def parse_date(value: str) -> datetime.date:
    v = value.strip().lower()
    today = datetime.date.today()
    if v == "today":
        return today
    if v == "tomorrow":
        return today + datetime.timedelta(days=1)
    try:
        return datetime.date.fromisoformat(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date '{value}' (use YYYY-MM-DD, today or tomorrow)") from None


def parse_duration(value: str) -> datetime.timedelta:
    if value.strip().isdigit():
        return datetime.timedelta(minutes=int(value))
    m = _DURATION_RE.match(value)
    if not m or not any(m.groups()):
        raise argparse.ArgumentTypeError(f"invalid duration '{value}' (try 20m, 1h30m, 90s)")
    d, h, mi, s = (int(g) if g else 0 for g in m.groups())
    return datetime.timedelta(days=d, hours=h, minutes=mi, seconds=s)


def parse_repeat(value: str) -> "Recurrence | None":
    try:
        return Recurrence.parse(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _resolve_when(args):
    if getattr(args, "in_", None) is not None:
        if args.time or args.date:
            raise ValueError("--in cannot be combined with --time/--date")
        return args.in_
    if args.time is None:
        if args.date:
            raise ValueError("--date requires --time")
        return None
    if args.date:
        rep = getattr(args, "repeat", None)
        if rep and rep.freq != "MONTHLY":
            raise ValueError("--date can't be combined with --repeat (the first matching day is used)")
        if rep and rep.days and str(args.date.day) not in rep.days:
            raise ValueError(f"--date {args.date} isn't on one of the repeat's days ({rep.describe()})")
        return datetime.datetime.combine(args.date, args.time)
    return args.time


# ---------------------------------------------------------------------- output

def _table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    out = [fmt.format(*headers), fmt.format(*("-" * w for w in widths))]
    out += [fmt.format(*map(str, r)) for r in rows]
    return "\n".join(out).rstrip()


def _print_items(items: list[Item], as_json: bool) -> None:
    if as_json:
        print(json.dumps([{"kind": i.kind.value, **i.raw} for i in items], indent=2))
        return
    if not items:
        print("No alarms, reminders or timers found.")
        return
    order = {k: n for n, k in enumerate(Kind)}
    items = sorted(items, key=lambda i: (order[i.kind], i.time or datetime.datetime.max))
    rows = [[i.shortId, i.kind.value, i.status, i.when_str(), describe(i.recurrence), i.deviceName,
             _unquote(i.label_str())] for i in items]
    print(_table(rows, ["ID", "TYPE", "STATUS", "WHEN", "REPEAT", "DEVICE", "LABEL / SOUND"]))


def _unquote(s: str) -> str:
    """'"Take medicine"' -> 'Take medicine'; leaves '"Pasta" 1:00:00 total' alone."""
    return s[1:-1] if len(s) >= 2 and s[0] == s[-1] == '"' and s.count('"') == 2 else s


# ---------------------------------------------------------------------- commands

def cmd_devices(alexa: AlexaAlerts, args) -> None:
    devs = alexa.devices() if args.all else [d for d in alexa.devices() if d.supportsAlarms or d.supportsReminders]
    if args.json:
        print(json.dumps([d.raw for d in devs], indent=2))
        return
    rows = [[d.name, d.family or "", "yes" if d.online else "no", "yes" if d.supportsAlarms else "",
             "yes" if d.supportsReminders else "", d.serial] for d in sorted(devs, key=lambda d: d.name.casefold())]
    print(_table(rows, ["NAME", "FAMILY", "ONLINE", "ALARMS/TIMERS", "REMINDERS", "SERIAL"]))


def cmd_list(alexa: AlexaAlerts, args) -> None:
    kind = None if args.type == "all" else args.type
    enabled = None if args.all else True
    _print_items(list(alexa.find(kind, device=args.device, enabled=enabled, query=args.query)), args.json)


def cmd_sounds(alexa: AlexaAlerts, args) -> None:
    alert_type = args.kind.capitalize()
    sounds = alexa.sounds(args.device, alert_type)
    if args.json:
        print(json.dumps([s.raw for s in sounds], indent=2))
        return
    try:
        current = alexa.defaultSound(args.device, alert_type)
    except AlexaAPIError:
        current = None
    default = current.id if current else None
    rows = [[s.name, s.id, (s.folder or "").replace("FOLDER_", "").lower(),
             "skill (unsupported)" if s.isSkill else ("current default" if s.id == default else "")] for s in sounds]
    print(_table(rows, ["NAME", "ID", "FOLDER", "NOTE"]))


def cmd_alarm(alexa: AlexaAlerts, args) -> None:
    when = _resolve_when(args)
    if when is None:
        raise ValueError("give --time (optionally --date) or --in")
    print(f"Created {alexa.createAlarm(args.device, when, recurrence=args.repeat, sound=args.sound, enabled=not args.off)}")


def cmd_reminder(alexa: AlexaAlerts, args) -> None:
    when = _resolve_when(args)
    if when is None:
        raise ValueError("give --time (optionally --date) or --in")
    print(f"Created {alexa.createReminder(args.device, when, args.text, recurrence=args.repeat, note=args.note, important=args.important, keep_reminding=args.keep_reminding)}")


def cmd_timer(alexa: AlexaAlerts, args) -> None:
    duration = args.duration
    timer = alexa.createTimer(args.device, duration, label=args.label, wait=0 if args.no_wait else args.wait)
    if args.no_wait:
        print("Sent the timer command; run `pyalexaalerts list --type timer` to check it.")
        return
    if timer is None:
        raise RuntimeError("The command was sent but no new timer appeared. Alexa may not have understood it.")
    print(f"Created {timer}")
    if abs(timer.duration - duration) > datetime.timedelta(seconds=1):
        print(f"warning: asked for {duration}, Alexa set {timer.duration}", file=sys.stderr)


def cmd_on(alexa: AlexaAlerts, args) -> None:
    for ref in args.ids:
        item = alexa.enable(ref)
        print(f"{'Resumed' if isinstance(item, Timer) else 'Switched on'} {item}")


def cmd_off(alexa: AlexaAlerts, args) -> None:
    for ref in args.ids:
        print(f"Switched off {alexa.disable(ref)} (kept; `on` switches it back on)")


def cmd_pause(alexa: AlexaAlerts, args) -> None:
    for ref in args.ids:
        print(f"Paused {alexa.pause(ref)}")


def cmd_resume(alexa: AlexaAlerts, args) -> None:
    for ref in args.ids:
        print(f"Resumed {alexa.resume(ref)}")


def cmd_stop(alexa: AlexaAlerts, args) -> None:
    alexa.stopAlert(args.device)
    print(f'Sent "stop" to {alexa.getDevice(args.device).name}')


def cmd_snooze(alexa: AlexaAlerts, args) -> None:
    alexa.snooze(args.device, args.duration)
    print(f"Sent snooze to {alexa.getDevice(args.device).name}")


def cmd_adjust_timer(alexa: AlexaAlerts, args) -> None:
    delta = args.duration if args.command == "add-time" else -args.duration
    timer = alexa.adjustTimer(args.id, delta)
    print(f"{'Added' if args.command == 'add-time' else 'Subtracted'} {_clock(args.duration)}: {timer}")


def _clock(delta: datetime.timedelta) -> str:
    s = round(delta.total_seconds())
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def cmd_edit(alexa: AlexaAlerts, args) -> None:
    item = alexa.get(args.id)
    if args.next_only:
        return _edit_next_occurrence(alexa, item, args)
    kwargs = {}
    when = _resolve_when(args)
    if when is not None:
        kwargs["when"] = when
    if args.repeat_given:
        kwargs["recurrence"] = args.repeat
    if args.text is not None:
        if item.kind is not Kind.REMINDER:
            raise ValueError("--text only applies to reminders")
        kwargs["text"] = args.text
    if args.sound is not None:
        if item.kind is not Kind.ALARM:
            raise ValueError("--sound only applies to alarms")
        kwargs["sound"] = args.sound
    for opt, key, value in (("--note", "note", args.note), ("--important", "important", args.important),
                            ("--keep-reminding", "keep_reminding", args.keep_reminding)):
        if value is not None:
            if item.kind is not Kind.REMINDER:
                raise ValueError(f"{opt} only applies to reminders")
            kwargs[key] = None if key == "note" and value.strip().lower() in ("", "none") else value
    if not kwargs:
        raise ValueError("nothing to change (use --time/--date/--in/--repeat/--text/--sound/--note/--important/"
                         "--keep-reminding)")
    print(f"Updated {alexa.update(item, **kwargs)}")


def _edit_next_occurrence(alexa: AlexaAlerts, item, args) -> None:
    if item.kind is not Kind.REMINDER:
        raise ValueError("--next-only only applies to repeating reminders")
    if args.repeat_given or args.sound is not None or any(
            v is not None for v in (args.note, args.important, args.keep_reminding)):
        raise ValueError("--next-only can change the time, date or text, not --repeat/--sound/--note/flags")
    if args.in_ is not None:
        if args.time or args.date:
            raise ValueError("--in cannot be combined with --time/--date")
        when = args.in_
    elif args.date and args.time:
        when = datetime.datetime.combine(args.date, args.time)
    else:
        when = args.time or args.date  # time: same day; date: same time; None: text only
    before = item.time
    new = alexa.updateReminderOccurrence(item, when=when, text=args.text)
    print(f"Changed the {before:%a %Y-%m-%d %H:%M} occurrence of {item.shortId}; it's now {new}")


def cmd_delete(alexa: AlexaAlerts, args) -> None:
    targets = [alexa.get(ref) for ref in args.ids]
    if not args.yes:
        for i in targets:
            print(f"  {i}")
        if input(f"Delete {len(targets)} item(s)? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return
    for i in targets:
        alexa.delete(i)
        print(f"Deleted {i}")


# ---------------------------------------------------------------------- parser

class _RepeatAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        namespace.repeat_given = True


def _list_type(value: str) -> str:
    if value.strip().lower() == "all":
        return "all"
    try:
        return Kind.from_str(value).value
    except ValueError:
        return value  # argparse then reports the valid choices


def _add_when_args(p: argparse.ArgumentParser, repeat_help: str) -> None:
    p.add_argument("--time", "-t", type=parse_time, help="time of day, e.g. 07:30, 7:30pm")
    p.add_argument("--date", "-D", type=parse_date, help="YYYY-MM-DD, today or tomorrow (default: next occurrence)")
    p.add_argument("--in", dest="in_", type=parse_duration, metavar="DURATION", help="relative time, e.g. 20m, 1h30m")
    p.add_argument("--repeat", "-r", type=parse_repeat, action=_RepeatAction, help=repeat_help)
    p.set_defaults(repeat=None, repeat_given=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyalexaalerts",
        description="Manage Alexa alarms, timers and reminders: create, edit and delete them, switch alarms off and on, "
                    "pause, resume and lengthen or shorten timers, and stop or snooze whatever is ringing.",
        epilog="Examples:" + __doc__.split("Examples:", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", "-c", type=Path, help="path to service_auth.json (default: ./config/service_auth.json)")
    parser.add_argument("--domain", default="amazon.com", choices=SUPPORTED_DOMAINS, metavar="DOMAIN",
                        help="Amazon region, e.g. amazon.co.uk (default: amazon.com)")
    parser.add_argument("--host", help="override the Alexa web API host (default: na-api-alexa.amazon.com for amazon.com)")
    parser.add_argument("--person-id", help="your amzn1.account.… id, if it can't be looked up automatically")
    parser.add_argument("--verbose", "-v", action="count", default=0)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    repeat = ("daily, weekdays, weekends, days like mon,wed,sun, or (reminders) monthly / monthly:15 "
              "('none' to clear when editing)")

    p = sub.add_parser("devices", help="list Echo devices")
    p.add_argument("--all", "-a", action="store_true", help="include devices without alarm/reminder support")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_devices, needs=set())

    p = sub.add_parser("list", aliases=["ls"], help="list alarms, reminders and timers")
    p.add_argument("--type", "-T", type=_list_type, choices=["alarm", "reminder", "timer", "task", "all"],
                   default="all", help="alarm, reminder, timer, all (those three), or task for Alexa to-do items, "
                                       "which are only listed when asked for (plurals work too)")
    p.add_argument("--device", "-d", help="only this device (name or serial)")
    p.add_argument("--query", "-q", help="reminder text / timer label contains this")
    p.add_argument("--all", "-a", action="store_true", help="include switched-off alarms, finished reminders "
                                                                 "and done to-do items")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list, needs=ALL)

    p = sub.add_parser("sounds", help="list the alarm (or timer) sounds a device offers")
    p.add_argument("--device", "-d", required=True)
    p.add_argument("--for", dest="kind", choices=["alarm", "timer"], default="alarm",
                   help="which sound list (default: alarm)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sounds, needs=set())

    p = sub.add_parser("alarm", help="create an alarm")
    p.add_argument("--device", "-d", required=True, help="device name or serial")
    _add_when_args(p, repeat)
    p.add_argument("--sound", "-s", help="sound name or id from `sounds` (default: the device default)")
    p.add_argument("--off", action="store_true", help="create it switched off")
    p.set_defaults(func=cmd_alarm, needs={Kind.ALARM})

    p = sub.add_parser("reminder", help="create a reminder")
    p.add_argument("--device", "-d", required=True, help="device name or serial")
    p.add_argument("--text", "-m", required=True, help="what Alexa should remind you about")
    _add_when_args(p, repeat)
    p.add_argument("--note", "-n", help="extra details stored with the reminder")
    p.add_argument("--important", action="store_true", help="flag it as important")
    p.add_argument("--keep-reminding", action="store_true",
                   help="have Alexa follow up until you confirm it's done (Alexa's follow-up setting)")
    p.set_defaults(func=cmd_reminder, needs={Kind.REMINDER})

    p = sub.add_parser("timer", help="start a timer (sent to the device as a text command)")
    p.add_argument("--device", "-d", required=True, help="device name or serial")
    p.add_argument("duration", type=parse_duration, help="e.g. 2m, 1h30m, 90s (a bare number is minutes)")
    p.add_argument("--label", "-l", help='timer name, e.g. "pizza"')
    p.add_argument("--wait", type=float, default=15, help="seconds to wait for the timer to appear (default 15)")
    p.add_argument("--no-wait", action="store_true", help="send the command and exit")
    p.set_defaults(func=cmd_timer, needs=set())

    p = sub.add_parser("on", aliases=["enable", "start"], help="switch alarm(s) back on")
    p.add_argument("ids", nargs="+", metavar="ID")
    p.set_defaults(func=cmd_on, needs={Kind.ALARM, Kind.TIMER})

    p = sub.add_parser("off", aliases=["disable"],
                       help="switch alarm(s) off but keep them (doesn't silence a ringing alarm)")
    p.add_argument("ids", nargs="+", metavar="ID")
    p.set_defaults(func=cmd_off, needs={Kind.ALARM, Kind.TIMER})

    p = sub.add_parser("pause", help="pause running timer(s)")
    p.add_argument("ids", nargs="+", metavar="ID")
    p.set_defaults(func=cmd_pause, needs={Kind.TIMER})

    p = sub.add_parser("resume", help="resume paused timer(s)")
    p.add_argument("ids", nargs="+", metavar="ID")
    p.set_defaults(func=cmd_resume, needs={Kind.TIMER})

    for name, verb in (("add-time", "add time to"), ("subtract-time", "take time off")):
        p = sub.add_parser(name, help=f"{verb} a running or paused timer")
        p.add_argument("id", metavar="ID")
        p.add_argument("duration", type=parse_duration, help="e.g. 5m, 30s, 1h (a bare number is minutes)")
        p.set_defaults(func=cmd_adjust_timer, needs={Kind.TIMER})

    p = sub.add_parser("stop-alert", help='silence what\'s ringing on a device, like "Alexa, stop"')
    p.add_argument("--device", "-d", required=True, help="device name or serial")
    p.set_defaults(func=cmd_stop, needs=set())

    p = sub.add_parser("snooze", help='snooze the alarm or reminder ringing on a device, like "Alexa, snooze"')
    p.add_argument("--device", "-d", required=True, help="device name or serial")
    p.add_argument("--for", dest="duration", type=parse_duration, help="e.g. 5m (default: the device's snooze length)")
    p.set_defaults(func=cmd_snooze, needs=set())

    p = sub.add_parser("edit", help="change an alarm's time/repeat/sound or a reminder's time/repeat/text")
    p.add_argument("id", metavar="ID")
    _add_when_args(p, repeat)
    p.add_argument("--text", "-m", help="new reminder text")
    p.add_argument("--sound", "-s", help="new alarm sound")
    p.add_argument("--note", "-n", help="new reminder note ('none' removes it)")
    p.add_argument("--important", action=argparse.BooleanOptionalAction, default=None,
                   help="flag (or with --no-important, unflag) a reminder as important")
    p.add_argument("--keep-reminding", action=argparse.BooleanOptionalAction, default=None,
                   help="switch a reminder's follow-up on (or off with --no-keep-reminding)")
    p.add_argument("--next-only", action="store_true",
                   help="repeating reminder: change only its next occurrence (-t/-D/--in/-m); the series stays as is")
    p.set_defaults(func=cmd_edit, needs={Kind.ALARM, Kind.REMINDER})

    p = sub.add_parser("delete", aliases=["rm", "cancel"], help="delete alarm(s), reminder(s) or timer(s)")
    p.add_argument("ids", nargs="+", metavar="ID")
    p.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    p.set_defaults(func=cmd_delete, needs=ALL)

    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING - 10 * min(args.verbose, 2), format="%(levelname)s %(name)s: %(message)s")
    try:
        alexa = AlexaAlerts(amazon_domain=args.domain, service_auth_path=args.config, alexa_host=args.host,
                                   person_id=args.person_id)
        alexa.resync(args.needs)
        args.func(alexa, args)
    except (DeviceNotFoundException, NotificationNotFoundException, ValueError, LookupError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (AlexaAPIError, AlexaSessionExpiredException, FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        if "TRIGGER_SCHEDULED_TIME_CONFLICT" in str(e):
            print("hint: that device already has an alarm at that time (repeating alarms count on every "
                  "matching day); pick another time or edit the existing alarm", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
