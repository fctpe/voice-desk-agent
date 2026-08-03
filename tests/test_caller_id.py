"""SIP caller-ID extraction — the one identity a caller cannot choose.

No trunk is wired in this repo (docs/telephony.md), so the participant is
stubbed. What is worth pinning down is that the caller-ID is *waited* for rather
than raced, and that each way of not getting one leaves the call unidentified
with a log saying which way it was.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from livekit import rtc

from voice_desk import agent
from voice_desk.agent import _caller_id


class StubJobContext:
    """Enough JobContext for ``_caller_id``: a SIP participant that joins late.

    ``JobContext.wait_for_participant`` blocks until a matching participant
    appears rather than reporting its absence, so the stub blocks too — that
    behaviour is the whole reason for using it over ``room.remote_participants``.
    """

    def __init__(self, participant=None, *, join_after: float = 0.0) -> None:
        self._participant = participant
        self._join_after = join_after
        self.waited = False

    async def wait_for_participant(self, *, kind) -> SimpleNamespace:
        self.waited = True
        await asyncio.sleep(self._join_after)
        if self._participant is None or self._participant.kind not in kind:
            await asyncio.Event().wait()  # no matching participant ever joins
        return self._participant


def _sip(attributes: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(
        identity="sip_caller",
        kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
        attributes=attributes,
    )


async def test_caller_id_waits_for_a_late_sip_participant():
    """Regression: the room's participant list syncs after ctx.connect(), so
    reading it on the next line lost the race and yielded no caller-ID at all."""
    ctx = StubJobContext(_sip({"sip.phoneNumber": "+1 555 0147"}), join_after=0.05)
    assert await _caller_id(ctx, expect_trunk=True) == "+1 555 0147"


async def test_no_trunk_does_not_wait(caplog):
    caplog.set_level(logging.INFO, logger="voice_desk")
    ctx = StubJobContext()
    assert await _caller_id(ctx, expect_trunk=False) is None
    assert ctx.waited is False
    assert "SIP_TRUNK is off" in caplog.text


async def test_trunk_without_a_sip_participant_fails_closed(caplog, monkeypatch):
    """Distinguishable from 'no trunk': this one is a configured trunk whose
    caller-ID never arrived, which is an error, not the console."""
    caplog.set_level(logging.ERROR, logger="voice_desk")
    monkeypatch.setattr(agent, "_SIP_PARTICIPANT_TIMEOUT", 0.05)
    ctx = StubJobContext()
    assert await _caller_id(ctx, expect_trunk=True) is None
    assert "no SIP participant joined" in caplog.text


async def test_sip_participant_without_caller_id_fails_closed(caplog):
    caplog.set_level(logging.ERROR, logger="voice_desk")
    ctx = StubJobContext(_sip({}))
    assert await _caller_id(ctx, expect_trunk=True) is None
    assert "sip.phoneNumber" in caplog.text
