"""In-memory, deterministic fakes for the calendar and contacts book.

Everything is seeded from a single integer so a given seed always produces the
same eight contacts and the same two-week grid of appointment slots (with
realistic gaps where the business is already booked). Two demo knobs make the
fakes useful for showing resilience:

* ``latency_ms`` — an artificial delay on every call, to exercise the agent's
  async latency-masking (spoken fillers while a tool is "thinking").
* ``fail_mode`` — inject a backend outage on a chosen surface so you can hear the
  agent degrade gracefully instead of crashing.

No network, no clock surprises: ``now`` is injectable for reproducible tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from datetime import date, datetime, time, timedelta

from .base import (
    Booking,
    BookingNotFoundError,
    Contact,
    ProviderError,
    Slot,
    SlotUnavailableError,
)

# Business hours (see prompts.py for the caller-facing statement of these).
_WEEKDAY_HOURS = (time(8, 0), time(18, 0))  # Mon–Fri 08:00–18:00
_SATURDAY_HOURS = (time(9, 0), time(14, 0))  # Sat 09:00–14:00
_SLOT_MINUTES = 60
_HORIZON_DAYS = 14
# Fraction of grid slots that are already booked out (creates realistic gaps).
_BOOKED_FRACTION = 0.45

_SEED_CONTACTS = [
    ("Sarah Nguyen", "+1 555 0102", "Regular customer, prefers morning visits"),
    ("David Thompson", "+1 555 0111", "Boiler serviced last winter"),
    ("Priya Patel", "+1 555 0119", ""),
    ("Marcus Bell", "+1 555 0123", "Landlord, multiple properties"),
    ("Elena Rossi", "+1 555 0128", "Kitchen remodel in progress"),
    ("James O'Connor", "+1 555 0131", ""),
    ("Aisha Rahman", "+1 555 0140", "New customer since spring"),
    ("Tom Fletcher", "+1 555 0147", "Recurring drain maintenance"),
]


def _digits(phone: str) -> str:
    """Reduce a phone number to its trailing digits for forgiving matching."""
    return "".join(ch for ch in phone if ch.isdigit())[-10:]


class MemoryContacts:
    """Fake contacts book keyed by phone number."""

    def __init__(self, *, seed: int = 42, latency_ms: int = 0, fail_mode: str = "none") -> None:
        self._latency = max(0, latency_ms) / 1000.0
        self._fail_mode = fail_mode
        rng = random.Random(seed)
        contacts = list(_SEED_CONTACTS)
        rng.shuffle(contacts)
        self._by_phone: dict[str, Contact] = {
            _digits(phone): Contact(name=name, phone=phone, notes=notes)
            for name, phone, notes in contacts
        }

    @property
    def contacts(self) -> list[Contact]:
        return list(self._by_phone.values())

    async def lookup(self, phone: str) -> Contact | None:
        await self._tick()
        if self._fail_mode == "contacts":
            raise ProviderError("contacts backend unavailable")
        return self._by_phone.get(_digits(phone))

    async def _tick(self) -> None:
        if self._latency:
            await asyncio.sleep(self._latency)


class MemoryCalendar:
    """Fake calendar with a deterministic two-week grid of slots."""

    def __init__(
        self,
        *,
        seed: int = 42,
        latency_ms: int = 0,
        fail_mode: str = "none",
        now: datetime | None = None,
    ) -> None:
        self._latency = max(0, latency_ms) / 1000.0
        self._fail_mode = fail_mode
        self._now = now or datetime.now()
        self._rng = random.Random(seed)
        self._seed = seed

        self._slots: dict[str, Slot] = {}
        self._booked: set[str] = set()
        self._bookings: dict[str, Booking] = {}  # booking_id -> Booking
        self._by_phone: dict[str, str] = {}  # phone digits -> booking_id
        self._build_grid()

    # -- grid construction --------------------------------------------------
    def _slot_id(self, start: datetime) -> str:
        raw = f"{self._seed}:{start.isoformat()}".encode()
        return "slot-" + hashlib.sha1(raw).hexdigest()[:6]

    def _hours_for(self, day: date) -> tuple[time, time] | None:
        weekday = day.weekday()  # Mon=0 .. Sun=6
        if weekday <= 4:
            return _WEEKDAY_HOURS
        if weekday == 5:
            return _SATURDAY_HOURS
        return None  # closed Sunday

    def _build_grid(self) -> None:
        start_day = self._now.date()
        for offset in range(_HORIZON_DAYS):
            day = start_day + timedelta(days=offset)
            hours = self._hours_for(day)
            if hours is None:
                continue
            open_t, close_t = hours
            cursor = datetime.combine(day, open_t)
            end_of_day = datetime.combine(day, close_t)
            while cursor + timedelta(minutes=_SLOT_MINUTES) <= end_of_day:
                slot_end = cursor + timedelta(minutes=_SLOT_MINUTES)
                # Only offer future slots (keeps the demo honest about "today").
                already_booked = self._rng.random() < _BOOKED_FRACTION
                if cursor > self._now:
                    slot = Slot(id=self._slot_id(cursor), start=cursor, end=slot_end)
                    self._slots[slot.id] = slot
                    if already_booked:
                        self._booked.add(slot.id)
                cursor = slot_end

    # -- query --------------------------------------------------------------
    def _free_slots(self) -> list[Slot]:
        return sorted(
            (s for sid, s in self._slots.items() if sid not in self._booked),
            key=lambda s: s.start,
        )

    async def find_slots(
        self, *, day_preference: str, time_window: str, limit: int = 3
    ) -> list[Slot]:
        await self._tick()
        if self._fail_mode == "calendar":
            raise ProviderError("calendar backend unavailable")

        candidates = self._free_slots()
        candidates = [s for s in candidates if _matches_day(s, day_preference, self._now)]
        candidates = [s for s in candidates if _matches_window(s, time_window)]
        return candidates[:limit]

    async def get_slot(self, slot_id: str) -> Slot | None:
        await self._tick()
        return self._slots.get(slot_id)

    # -- mutation -----------------------------------------------------------
    async def book(self, *, slot_id: str, name: str, phone: str, issue: str) -> Booking:
        await self._tick()
        if self._fail_mode == "calendar":
            raise ProviderError("calendar backend unavailable")

        slot = self._slots.get(slot_id)
        if slot is None:
            raise SlotUnavailableError(f"unknown slot id: {slot_id}")
        if slot_id in self._booked:
            raise SlotUnavailableError(f"slot already booked: {slot_id}")

        self._booked.add(slot_id)
        booking = Booking(
            id="bk-" + hashlib.sha1(f"{slot_id}:{phone}".encode()).hexdigest()[:8],
            slot=slot,
            name=name,
            phone=phone,
            issue=issue,
        )
        self._bookings[booking.id] = booking
        self._by_phone[_digits(phone)] = booking.id
        return booking

    async def find_booking(self, phone: str) -> Booking | None:
        await self._tick()
        booking_id = self._by_phone.get(_digits(phone))
        if booking_id is None:
            return None
        booking = self._bookings.get(booking_id)
        if booking is None or booking.status == "cancelled":
            return None
        return booking

    async def reschedule(self, *, phone: str, new_slot_id: str) -> Booking:
        await self._tick()
        if self._fail_mode == "calendar":
            raise ProviderError("calendar backend unavailable")

        booking = await self.find_booking(phone)
        if booking is None:
            raise BookingNotFoundError(f"no active booking for {phone}")

        new_slot = self._slots.get(new_slot_id)
        if new_slot is None:
            raise SlotUnavailableError(f"unknown slot id: {new_slot_id}")
        if new_slot_id in self._booked:
            raise SlotUnavailableError(f"slot already booked: {new_slot_id}")

        self._booked.discard(booking.slot.id)  # release the old slot
        self._booked.add(new_slot_id)
        booking.slot = new_slot
        booking.status = "rescheduled"
        return booking

    async def cancel(self, phone: str) -> Booking:
        await self._tick()
        booking = await self.find_booking(phone)
        if booking is None:
            raise BookingNotFoundError(f"no active booking for {phone}")
        self._booked.discard(booking.slot.id)
        booking.status = "cancelled"
        return booking

    async def _tick(self) -> None:
        if self._latency:
            await asyncio.sleep(self._latency)


# -- fuzzy day/time matching -----------------------------------------------
_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _matches_day(slot: Slot, day_preference: str, now: datetime) -> bool:
    """Best-effort match of a free-text day hint against a slot's date.

    The LLM does the heavy lifting of turning "next Tuesday" into a target; this
    keeps the tool honest by only returning slots on a plausibly-matching day.
    An empty/"any"/"asap" hint matches everything (earliest-first ordering then
    surfaces the soonest option).
    """
    pref = (day_preference or "").strip().lower()
    if not pref or pref in ("any", "anytime", "asap", "soon", "earliest", "whenever", "flexible"):
        return True

    today = now.date()
    slot_day = slot.start.date()

    if "today" in pref:
        return slot_day == today
    if "tomorrow" in pref:
        return slot_day == today + timedelta(days=1)

    for name, weekday in _WEEKDAYS.items():
        if name in pref:
            days_ahead = (weekday - today.weekday()) % 7
            # "next <weekday>" or a same-day weekday name → the upcoming occurrence.
            if days_ahead == 0:
                days_ahead = 7
            target = today + timedelta(days=days_ahead)
            return slot_day == target

    if "week" in pref:  # "this week" / "next week" — keep it in the horizon
        return True

    return True  # unrecognised hint: don't over-filter, let ordering decide


def _matches_window(slot: Slot, time_window: str) -> bool:
    window = (time_window or "").strip().lower()
    hour = slot.start.hour
    if not window or window in ("any", "anytime", "flexible", "whenever"):
        return True
    if "morning" in window:
        return hour < 12
    if "afternoon" in window:
        return 12 <= hour < 17
    if "evening" in window:
        return hour >= 17
    return True
