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


async def test_identify_unknown_caller(agent, ctx):
    out = await agent.identify_caller(ctx, phone="+1 555 9999")
    assert "new caller" in out.lower()
    assert ctx.userdata.caller is None


async def test_cancel_without_booking(agent, ctx):
    out = await agent.cancel_appointment(ctx, phone="+1 555 0000")
    assert "No active appointment" in out
