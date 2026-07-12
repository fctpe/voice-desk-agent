"""Provider ports and their in-memory, offline implementations."""

from __future__ import annotations

from .base import (
    Booking,
    BookingNotFoundError,
    CalendarProvider,
    ConfirmationSink,
    Contact,
    ContactsProvider,
    Message,
    ProviderError,
    Slot,
    SlotUnavailableError,
)
from .ics import IcsConfirmationSink, render_ics
from .memory import MemoryCalendar, MemoryContacts

__all__ = [
    "Booking",
    "BookingNotFoundError",
    "CalendarProvider",
    "Contact",
    "ConfirmationSink",
    "ContactsProvider",
    "IcsConfirmationSink",
    "MemoryCalendar",
    "MemoryContacts",
    "Message",
    "ProviderError",
    "Slot",
    "SlotUnavailableError",
    "render_ics",
]
