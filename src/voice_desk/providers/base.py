"""Provider contracts and domain types.

The agent talks to three narrow ports — a calendar, a contacts book and a
confirmation sink — expressed as :class:`typing.Protocol` classes. Only the
fake, in-memory implementations ship in this repo (see ADR-0001): they make the
evals reproducible and let the whole test suite run offline. A production
deployment would provide real adapters (Google/Microsoft calendar, a CRM, an
email/SMS sender) satisfying the same Protocols with no change to the tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable


class ProviderError(Exception):
    """Base class for recoverable provider failures.

    Tools catch these and turn them into a calm spoken apology rather than a
    crash, which is what a caller should hear when a backend hiccups.
    """


class SlotUnavailableError(ProviderError):
    """A slot id was never issued, no longer exists, or is already taken."""


class BookingNotFoundError(ProviderError):
    """No existing booking matches the caller (used by reschedule/cancel)."""


@dataclass(frozen=True)
class Contact:
    """A known caller looked up by phone number."""

    name: str
    phone: str
    notes: str = ""


@dataclass(frozen=True)
class Slot:
    """An offerable appointment window.

    ``id`` is an opaque, server-issued token. The booking tool only accepts ids
    it previously handed out during the call (see the slot cache in
    :mod:`voice_desk.tools`), which is the primary defence against a model
    inventing a plausible-looking time.
    """

    id: str
    start: datetime
    end: datetime

    @property
    def day_label(self) -> str:
        # e.g. "Tuesday, July 15" (manual day-of-month to stay portable across libc).
        return f"{self.start:%A, %B} {self.start.day}"

    @property
    def time_label(self) -> str:
        # e.g. "9:00 AM"
        hour_12 = self.start.hour % 12 or 12
        meridiem = "AM" if self.start.hour < 12 else "PM"
        return f"{hour_12}:{self.start.minute:02d} {meridiem}"

    def spoken(self) -> str:
        """Human-readable phrase the agent can read back, e.g.
        'Tuesday, July 15 at 9:00 AM'."""
        return f"{self.day_label} at {self.time_label}"


@dataclass
class Booking:
    """A confirmed appointment."""

    id: str
    slot: Slot
    name: str
    phone: str
    issue: str
    status: str = "confirmed"  # "confirmed" | "rescheduled" | "cancelled"


@dataclass
class Message:
    """A message taken for the owner (escalation / callback request)."""

    name: str
    phone: str
    body: str
    urgent: bool = False
    taken_at: datetime = field(default_factory=datetime.now)


@runtime_checkable
class ContactsProvider(Protocol):
    """Read-only lookup of known callers by phone number."""

    async def lookup(self, phone: str) -> Contact | None: ...


@runtime_checkable
class CalendarProvider(Protocol):
    """Availability + booking lifecycle."""

    async def find_slots(
        self, *, day_preference: str, time_window: str, limit: int = 3
    ) -> list[Slot]: ...

    async def book(self, *, slot_id: str, name: str, phone: str, issue: str) -> Booking: ...

    async def find_booking(self, phone: str) -> Booking | None: ...

    async def reschedule(self, *, phone: str, new_slot_id: str) -> Booking: ...

    async def cancel(self, phone: str) -> Booking: ...


@runtime_checkable
class ConfirmationSink(Protocol):
    """Somewhere a booking confirmation is written/sent.

    The in-repo implementation writes an ``.ics`` file to the outbox; a real one
    might email or SMS it.
    """

    async def send(self, booking: Booking) -> str: ...
