"""Tools exercised directly against a stubbed RunContext (no LLM, no audio)."""

from __future__ import annotations

from pathlib import Path

import pytest

from voice_desk.config import Settings
from voice_desk.tools import FrontDeskAgent, build_desk_context


class StubRunContext:
    """Minimal stand-in for RunContext: tools only touch ``.userdata``."""

    def __init__(self, userdata):
        self.userdata = userdata


@pytest.fixture
def agent() -> FrontDeskAgent:
    return FrontDeskAgent()


@pytest.fixture
def ctx(tmp_path: Path, fixed_now):
    settings = Settings(outbox_dir=str(tmp_path / "outbox"))
    data = build_desk_context(settings, now=fixed_now)
    return StubRunContext(data)


async def _first_issued_slot_id(agent, ctx, **kw) -> str:
    await agent.check_availability(ctx, day_preference=kw.get("day", ""), time_window="any")
    assert ctx.userdata.issued_slots, "check_availability should issue at least one slot"
    return next(iter(ctx.userdata.issued_slots))


async def test_check_availability_issues_slots(agent, ctx):
    out = await agent.check_availability(ctx, day_preference="tomorrow", time_window="morning")
    assert ctx.userdata.issued_slots
    assert "slot_id" in out  # the ids are surfaced to the model, not the caller


async def test_book_rejects_unissued_slot(agent, ctx):
    # No check_availability yet -> the id was never offered.
    out = await agent.book_appointment(
        ctx, slot_id="slot-hallucinated", name="Al", phone="+1 555 0199", issue="leak"
    )
    assert "not among the times offered" in out
    assert ctx.userdata.bookings == []


async def test_book_accepts_issued_slot(agent, ctx):
    slot_id = await _first_issued_slot_id(agent, ctx)
    out = await agent.book_appointment(
        ctx, slot_id=slot_id, name="Al Carter", phone="+1 555 0199", issue="leaking tap"
    )
    assert "Booked" in out
    assert len(ctx.userdata.bookings) == 1
    assert ctx.userdata.bookings[0].name == "Al Carter"


async def test_reschedule_requires_issued_new_slot(agent, ctx):
    await agent.identify_caller(ctx, phone="+1 555 0199")
    slot_id = await _first_issued_slot_id(agent, ctx)
    await agent.book_appointment(ctx, slot_id=slot_id, name="Al", phone="+1 555 0199", issue="leak")
    out = await agent.reschedule_appointment(ctx, phone="+1 555 0199", new_slot_id="slot-madeup")
    assert "not among the times offered" in out


async def test_send_confirmation_writes_ics(agent, ctx, tmp_path):
    slot_id = await _first_issued_slot_id(agent, ctx)
    await agent.book_appointment(ctx, slot_id=slot_id, name="Al", phone="+1 555 0199", issue="leak")
    out = await agent.send_confirmation(ctx, phone="+1 555 0199")
    assert "Confirmation sent" in out
    assert ctx.userdata.confirmations_sent
    written = Path(ctx.userdata.confirmations_sent[0])
    assert written.exists() and written.suffix == ".ics"


async def test_send_confirmation_does_not_confirm_a_mismatched_number(agent, ctx):
    """Regression: send_confirmation must NOT fall back to some other active
    booking and falsely report success for a number that booked nothing."""
    slot_id = await _first_issued_slot_id(agent, ctx)
    await agent.book_appointment(ctx, slot_id=slot_id, name="Al", phone="+1 555 0199", issue="leak")
    out = await agent.send_confirmation(ctx, phone="+1 555 7777")  # never booked
    assert "no booking" in out.lower()
    assert not ctx.userdata.confirmations_sent


async def test_take_message_escalates(agent, ctx):
    out = await agent.take_message(
        ctx, name="Sam", phone="+1 555 0188", message="burst pipe", urgent=True
    )
    assert "Mr. Miller" in out
    assert len(ctx.userdata.messages) == 1
    assert ctx.userdata.messages[0].urgent is True


async def test_identify_known_caller(agent, ctx):
    known = ctx.userdata.contacts.contacts[0]
    out = await agent.identify_caller(ctx, phone=known.phone)
    assert "Returning customer" in out
    assert ctx.userdata.caller is not None
    assert ctx.userdata.caller.name == known.name
    assert ctx.userdata.authenticated_phone == known.phone


async def test_identify_unknown_caller(agent, ctx):
    out = await agent.identify_caller(ctx, phone="+1 555 9999")
    assert "new caller" in out.lower()
    assert ctx.userdata.caller is None
    # Being unknown to the contacts book is not the same as being unidentified:
    # the call is still bound to the number, so they can manage their own booking.
    assert ctx.userdata.authenticated_phone == "+1 555 9999"


async def test_cancel_without_booking(agent, ctx):
    await agent.identify_caller(ctx, phone="+1 555 0000")
    out = await agent.cancel_appointment(ctx, phone="+1 555 0000")
    assert "No active appointment" in out


async def test_identity_binds_once(agent, ctx):
    ctx.userdata.authenticated_phone = "+1 555 0102"  # as SIP caller-ID sets it
    await agent.identify_caller(ctx, phone="+1 555 0147")
    assert ctx.userdata.authenticated_phone == "+1 555 0102"


async def test_caller_stated_identity_is_refused_on_a_trunk(agent, ctx):
    """On a trunk, caller-ID is the identity. A call whose caller-ID never
    arrived must stay unidentified, not degrade to the caller's word for it."""
    ctx.userdata.allow_caller_stated_identity = False
    await agent.identify_caller(ctx, phone="+1 555 0147")
    assert ctx.userdata.authenticated_phone is None
    out = await agent.cancel_appointment(ctx, phone="+1 555 0147")
    # No dead end: identifying again cannot help here, so it points at the way out.
    assert "cannot get one" in out and "take a message" in out


async def test_booking_does_not_grant_identity(agent, ctx):
    """The escalation this closes: an unidentified caller books a throwaway slot
    under the victim's number, which used to bind that number as the call's
    identity and hand them the victim's cancel/reschedule rights."""
    victim = "+1 555 0111"
    await agent.check_availability(ctx, day_preference="", time_window="any")
    theirs, throwaway = list(ctx.userdata.issued_slots)[:2]
    booked = await ctx.userdata.calendar.book(
        slot_id=theirs, name="David Thompson", phone=victim, issue="boiler service"
    )

    await agent.book_appointment(
        ctx, slot_id=throwaway, name="Mallory", phone=victim, issue="anything"
    )
    assert ctx.userdata.authenticated_phone is None

    out = await agent.cancel_appointment(ctx, phone=victim)
    assert "no identified caller number" in out
    assert booked.status != "cancelled"


async def test_send_confirmation_needs_no_identity(agent, ctx):
    """The landlord-books-for-tenant flow the prompt promises: a booking made in
    this call is confirmable, whoever it is under. _latest_booking_for cannot
    reach past this call, so there is no stranger's appointment to leak."""
    await agent.identify_caller(ctx, phone="+1 555 0102")
    slot_id = await _first_issued_slot_id(agent, ctx)
    await agent.book_appointment(
        ctx, slot_id=slot_id, name="Tom Fletcher", phone="+1 555 0147", issue="drain"
    )
    out = await agent.send_confirmation(ctx, phone="+1 555 0147")
    assert "Confirmation sent" in out
    assert ctx.userdata.confirmations_sent


async def _booking_for_someone_else(agent, ctx) -> str:
    """Identify as one caller, then put a booking on a different number."""
    await agent.identify_caller(ctx, phone="+1 555 0102")
    slot_id = await _first_issued_slot_id(agent, ctx)
    await agent.book_appointment(
        ctx, slot_id=slot_id, name="Tom Fletcher", phone="+1 555 0147", issue="drain"
    )
    return slot_id


async def test_cancel_refuses_another_callers_number(agent, ctx):
    """The hole this closes: reciting a number is not authorization to cancel it."""
    await _booking_for_someone_else(agent, ctx)
    out = await agent.cancel_appointment(ctx, phone="+1 555 0147")
    assert "not this caller's own number" in out
    assert ctx.userdata.bookings[0].status != "cancelled"


async def test_reschedule_refuses_another_callers_number(agent, ctx):
    booked = await _booking_for_someone_else(agent, ctx)
    other = next(sid for sid in ctx.userdata.issued_slots if sid != booked)
    out = await agent.reschedule_appointment(ctx, phone="+1 555 0147", new_slot_id=other)
    assert "not this caller's own number" in out
    assert ctx.userdata.bookings[0].slot.id == booked


async def test_cancel_refuses_when_no_caller_is_identified(agent, ctx):
    """Fails closed: nothing bound means nothing to authorize against."""
    out = await agent.cancel_appointment(ctx, phone="+1 555 0147")
    assert "no identified caller number" in out
