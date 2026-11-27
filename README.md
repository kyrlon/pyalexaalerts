# pyalexaalerts

An unofficial Python library and command-line app for managing Amazon Alexa **alarms, timers and reminders** on your Echo devices:

- **Alarms:** create, edit, delete, and switch off and on.
- **Reminders:** create, edit and delete one-off, daily, weekly and monthly reminders, with notes, an important flag and keep reminding.
- **Timers:** create, pause, resume, add time to, take time off and delete.
- **Ringing:** stop or snooze whatever is going off.

It's a sister project of [pyalexalist](https://github.com/kyrlon/pyalexalist) and uses the same credential setup.

*pyalexaalerts is neither supported nor endorsed by Amazon.*

## Install

Python 3.10 or newer. From PyPI:

```
pip install pyalexaalerts
```

or, to install into a specific Python (e.g. when `pip` points at a different one):

```
python -m pip install pyalexaalerts
```

From a clone of the repository:

```
git clone https://github.com/kyrlon/pyalexaalerts.git
cd pyalexaalerts
pip install .
```

For development, install it editable with the test dependencies instead: `pip install -e .[test]`.

## Obtaining Alexa credentials

This is the same flow as pyalexalist. If you already have a `config/service_auth.json` from pyalexalist, copy it here or point `--config` at it. The app looks for `config/service_auth.json` in the folder you run it from.

1. Create `config/service_auth.json`:
   ```json
   {
       "alexa": {
           "refresh_token": ""
       }
   }
   ```
2. Download the cookie CLI: `pyalexaalerts-setup-cli` (supports `--list`, `--version X`, `--force`).
3. Sign in and copy the refresh token into `alexa.refresh_token`:
   ```
   .\alexa-cookie-cli\alexa-cookie-cli-win.exe -p amazon.com -b amazon.com -a en_US -L en-US   # Windows
   ./alexa-cookie-cli/alexa-cookie-cli-linux -p amazon.com -b amazon.com -a en_US -L en-US     # Linux
   ./alexa-cookie-cli/alexa-cookie-cli-macos -p amazon.com -b amazon.com -a en_US -L en-US     # macOS
   ```
4. Run any command. The app swaps the refresh token for session cookies and an access token and caches both in `config/runtime_credentials.json`. Both are renewed automatically when they expire or the server rejects them.

## Command-line app

`ID` below is the 8-character ID shown by `list`; any unique start of it works. Run `pyalexaalerts COMMAND --help` for every option.

```
pyalexaalerts devices                       # Echo devices
pyalexaalerts list                          # alarms (on), reminders, timers
pyalexaalerts list --all --type alarm       # include switched-off alarms
pyalexaalerts list --type task              # Alexa to-do items (add --all for done ones)
pyalexaalerts sounds -d bedroom             # alarm sounds for a device
pyalexaalerts sounds -d bedroom --for timer # timer sounds (the current default is marked)

# alarms
pyalexaalerts alarm -d "Bedroom Echo" -t 6:45 -r weekdays
pyalexaalerts alarm -d bedroom -t 7:00 -r mon,wed,sun -s "Sonar"
pyalexaalerts alarm -d bedroom -t 9am -D 2026-12-25
pyalexaalerts alarm -d bedroom --in 8h --off              # created switched off
pyalexaalerts off 1a2b3c4d                  # switch an alarm off, keep it (alias: disable)
pyalexaalerts on 1a2b3c4d                   # switch it back on (aliases: enable, start)
pyalexaalerts edit 1a2b3c4d -t 7:15 -r weekends -s "Simple Alarm"

# reminders
pyalexaalerts reminder -d kitchen --in 20m -m "Take the pizza out"
pyalexaalerts reminder -d kitchen -t 9pm -r tue,thu -m "Take out trash"
pyalexaalerts reminder -d kitchen -t 7pm -r monthly:2 -m "Give Bella her medicine"
pyalexaalerts reminder -d kitchen -t 8am -r daily -m "Take medicine" --keep-reminding --important
pyalexaalerts reminder -d kitchen -D 2026-12-10 -t 9am -m "Renew tags" -n "bring insurance card"
pyalexaalerts edit 5e6f7a8b -m "Bins out" -t 8pm
pyalexaalerts edit 5e6f7a8b -n "recycling too" --no-important --keep-reminding
pyalexaalerts edit 5e6f7a8b --next-only -t 9pm -m "Bins out late"    # only the next occurrence

# timers
pyalexaalerts timer -d kitchen 10m
pyalexaalerts timer -d kitchen 1h30m -l pizza
pyalexaalerts pause 9f8e7d6c
pyalexaalerts resume 9f8e7d6c
pyalexaalerts add-time 9f8e7d6c 5m
pyalexaalerts subtract-time 9f8e7d6c 2m

# whatever is ringing right now
pyalexaalerts stop-alert -d bedroom         # like "Alexa, stop"
pyalexaalerts snooze -d bedroom --for 5m    # alarm or reminder, like "Alexa, snooze for 5 minutes"

# delete anything
pyalexaalerts delete 1a2b3c4d 5e6f7a8b      # asks to confirm; -y to skip (aliases: rm, cancel)
```

`python -m pyalexaalerts ...` works too. Global options go **before** the command: `--config PATH`, `--domain amazon.co.uk`, `--host HOST`, `--person-id ID`, `-v`/`-vv` (logging).

- **Devices** match by serial number, then by exact name, then by a unique part of the name. Case is ignored.
- **When:** `-t` a time (`7:30`, `7:30pm`), optionally with `-D` a date (`2026-12-25`, `today`, `tomorrow`), or `--in` a duration (`20m`, `1h30m`). Times are the device's local time (see *Timezones* below).
- **Repeats** (`-r`): `daily`, `weekdays`, `weekends`, or any days like `mon,wed,sun` (full names work too). One alarm or reminder covers all the days. With a repeat and only a time, the first matching day is used. `-r none` clears a repeat when editing.
- **Monthly** (reminders only): `monthly:15`, or plain `monthly` for the day of the first occurrence, so `-D 2026-12-15 -r monthly` means the 15th of every month. `-D` works with monthly repeats but not with weekly ones.
- **Sounds:** a name or id from `sounds`. Built-in sounds work, including the celebrity ones.

## Programmatic usage

```python
import datetime
from pyalexaalerts import AlexaAlerts

alexa = AlexaAlerts()              # or service_auth_path=..., person_id=...
alexa.resync()

for item in alexa.find("alarm"):         # or "reminder", "timer"; "task" for to-do items
    print(item)

alarm = alexa.createAlarm("Bedroom Echo", datetime.time(6, 45), recurrence="weekdays", sound="Sonar")
alexa.disable(alarm)
alexa.enable(alarm)
alexa.updateAlarm(alarm, when=datetime.time(7, 0))

rem = alexa.createReminder("Kitchen", datetime.timedelta(minutes=20), "Take the pizza out",
                           note="middle rack", important=True, keep_reminding=True)
alexa.updateReminder(rem, note=None, important=False)

timer = alexa.createTimer("Kitchen", datetime.timedelta(minutes=2), label="pizza")   # None if it didn't appear
alexa.pause(timer)
alexa.resume(timer)
timer = alexa.adjustTimer(timer, datetime.timedelta(minutes=5))    # negative subtracts

alexa.stopAlert("Kitchen")         # whatever is ringing; alexa.snooze("Bedroom") for alarms or reminders
alexa.delete(rem)
```

`when` takes a `datetime` (device-local), a `time` (next matching occurrence) or a `timedelta` (from now). `AlexaAPI` is the low-level client and returns raw dicts.

## Notes and limitations

### Alarms
- **`off` doesn't silence a ringing alarm.** It switches the alarm off for the future and keeps it; `on` switches it back on. Use `stop-alert` to silence one that's ringing.
- **`cancel` deletes**, as in "Alexa, cancel my alarm".
- **Two alarms can't share a time on one device**, and a repeating alarm counts on every matching day.

### Reminders
- **No on/off switch.** Delete them instead. Deleting a repeating reminder removes the whole series.
- **`edit --next-only`** changes just the next occurrence of a repeating reminder: `-t` keeps the day, `-D` keeps the time, and `-m` changes the text.
- **Monthly reminders repeat on one day of the month.**
- **Finished one-off reminders** show as DONE in `list --all` and are hidden from plain `list`.
- **To-do items** from your Alexa to-do list are hidden unless you ask for them: `list --type task` shows open ones, and `--all` adds done ones. They can only be listed, not created, edited or deleted.
- **Not supported:** choosing a reminder's sound, location-based reminders, reminders for other people, and announcements to all devices.

### Timers
- **`add-time` / `subtract-time` are silent.** They pause the timer with the new time left and resume it straight away. A paused timer stays paused. Each change adds about a second, because the timer stops counting for an instant. You can't subtract more than is left.
- **Not supported:** renaming a timer, or giving one its own sound (new timers use the device's default timer sound).

### Ringing alerts
- **`stop-alert` silences whatever is ringing**, and **`snooze`** snoozes a ringing alarm or reminder. A snoozed reminder shows as SNOOZED in `list` until it goes off again.

### Repeats and sounds
- **Repeats** can be daily, weekdays, weekends, specific days, or (reminders only) monthly on one day. Not supported: monthly alarms, every N weeks or months, "first Monday"-style rules, yearly, or end dates. Daily reminders are saved as weekly on all 7 days, which works the same.
- **Skill sounds** (e.g. Loud Buzzer) are refused, because choosing one was never seen in the app and some cost money. Timer-only sounds can't be used for alarms.
- **Device default sounds** for alarms and timers can be shown (`sounds`) but not changed.

### General
- **Timezones:** alarms send a local time and use the Echo's timezone. One-off reminders are converted using this computer's timezone, so it should match the Echo's.
