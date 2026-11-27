"""An unofficial client for Amazon Alexa alarms, reminders and timers."""

__version__ = "0.1.0"

from .api import AlexaAPI
from .exception import (
    AlexaAPIError,
    AlexaSessionExpiredException,
    DeviceNotFoundException,
    NotificationNotFoundException,
)
from .manager import AlexaAlerts
from .resource import Alarm, Device, Item, Kind, Recurrence, Reminder, Sound, Task, Timer

__all__ = [
    "Alarm",
    "AlexaAPI",
    "AlexaAPIError",
    "AlexaAlerts",
    "AlexaSessionExpiredException",
    "Device",
    "DeviceNotFoundException",
    "Item",
    "Kind",
    "NotificationNotFoundException",
    "Recurrence",
    "Reminder",
    "Sound",
    "Task",
    "Timer",
]
