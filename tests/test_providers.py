"""Provider behaviour: slot issuing, double-booking rejection, failure injection."""

from __future__ import annotations

import time

import pytest

from voice_desk.providers import (
    BookingNotFoundError,
    ProviderError,
    SlotUnavailableError,
)
from voice_desk.providers.memory import MemoryCalendar, MemoryContacts


def _calendar(fixed_now, **kw) -> MemoryCalendar:
    return MemoryCalendar(seed=42, now=fixed_now, **kw)


async def test_grid_is_deterministic(fixed_now):
    a = _calendar(fixed_now)
    b = _calendar(fixed_now)
    ids_a = sorted(s.id for s in await a.find_slots(day_preference="", time_window="any", limit=99))
    ids_b = sorted(s.id for s in await b.find_slots(day_preference="", time_window="any", limit=99))
    assert ids_a == ids_b
    assert ids_a, "expected some free slots in the two-week horizon"


async def test_grid_has_gaps(fixed_now):
    cal = _calendar(fixed_now)
    free = await cal.find_slots(day_preference="", time_window="any", limit=999)
    # Total grid slots include the booked-out ones; some must be missing (gaps).
    total_grid = len(cal._slots)
    assert 0 < len(free) < total_grid


async def test_no_slots_on_sunday(fixed_now):
    cal = _calendar(fixed_now)
    # 2026-07-13 is a Monday; the following Sunday is 2026-07-19.
    slots = await cal.find_slots(day_preference="Sunday", time_window="any", limit=99)
    assert slots == []


async def test_morning_filter(fixed_now):
    cal = _calendar(fixed_now)
    slots = await cal.find_slots(day_preference="", time_window="morning", limit=99)
    assert slots and all(s.start.hour < 12 for s in slots)


async def test_book_then_double_book_rejected(fixed_now):
    cal = _calendar(fixed_now)
    slot = (await cal.find_slots(day_preference="", time_window="any", limit=1))[0]
    booking = await cal.book(slot_id=slot.id, name="Al", phone="+1 555 0199", issue="leak")
    assert booking.status == "confirmed"
    with pytest.raises(SlotUnavailableError):
        await cal.book(slot_id=slot.id, name="Bo", phone="+1 555 0100", issue="leak")


async def test_book_unknown_slot_rejected(fixed_now):
    cal = _calendar(fixed_now)
    with pytest.raises(SlotUnavailableError):
        await cal.book(slot_id="slot-doesnotexist", name="Al", phone="+1 555 0199", issue="x")


async def test_booked_slot_not_offered_again(fixed_now):
    cal = _calendar(fixed_now)
    slot = (await cal.find_slots(day_preference="", time_window="any", limit=1))[0]
    await cal.book(slot_id=slot.id, name="Al", phone="+1 555 0199", issue="leak")
    remaining = await cal.find_slots(day_preference="", time_window="any", limit=999)
    assert slot.id not in {s.id for s in remaining}


async def test_reschedule_moves_and_frees(fixed_now):
    cal = _calendar(fixed_now)
    first, second = await cal.find_slots(day_preference="", time_window="any", limit=2)
    await cal.book(slot_id=first.id, name="Al", phone="+1 555 0199", issue="leak")
    booking = await cal.reschedule(phone="+1 555 0199", new_slot_id=second.id)
    assert booking.slot.id == second.id
    assert booking.status == "rescheduled"
    # The original slot is free again and offerable.
    free_ids = {s.id for s in await cal.find_slots(day_preference="", time_window="any", limit=999)}
    assert first.id in free_ids


async def test_reschedule_without_booking_raises(fixed_now):
    cal = _calendar(fixed_now)
    slot = (await cal.find_slots(day_preference="", time_window="any", limit=1))[0]
    with pytest.raises(BookingNotFoundError):
        await cal.reschedule(phone="+1 555 0000", new_slot_id=slot.id)


async def test_cancel_frees_slot(fixed_now):
    cal = _calendar(fixed_now)
    slot = (await cal.find_slots(day_preference="", time_window="any", limit=1))[0]
    await cal.book(slot_id=slot.id, name="Al", phone="+1 555 0199", issue="leak")
    cancelled = await cal.cancel("+1 555 0199")
    assert cancelled.status == "cancelled"
    assert await cal.find_booking("+1 555 0199") is None


async def test_calendar_failure_injection(fixed_now):
    cal = _calendar(fixed_now, fail_mode="calendar")
    with pytest.raises(ProviderError):
        await cal.find_slots(day_preference="", time_window="any")


async def test_latency_injection_delays(fixed_now):
    cal = _calendar(fixed_now, latency_ms=25)
    start = time.perf_counter()
    await cal.find_slots(day_preference="", time_window="any")
    assert time.perf_counter() - start >= 0.02


async def test_contacts_lookup_and_failure():
    contacts = MemoryContacts(seed=42)
    known = contacts.contacts[0]
    found = await contacts.lookup(known.phone)
    assert found is not None and found.name == known.name
    assert await contacts.lookup("+1 555 9999") is None

    failing = MemoryContacts(seed=42, fail_mode="contacts")
    with pytest.raises(ProviderError):
        await failing.lookup(known.phone)
