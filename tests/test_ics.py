"""ICS confirmation output correctness."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from voice_desk.providers import ProviderError
from voice_desk.providers.base import Booking, Slot
from voice_desk.providers.ics import IcsConfirmationSink, render_ics


def _booking(issue: str = "leaking tap") -> Booking:
    start = datetime(2026, 7, 14, 9, 0)
    end = datetime(2026, 7, 14, 10, 0)
    slot = Slot(id="slot-abc123", start=start, end=end)
    return Booking(id="bk-test01", slot=slot, name="Al Carter", phone="+1 555 0199", issue=issue)


def test_render_ics_structure():
    text = render_ics(
        _booking(),
        business_name="Miller Plumbing & Heating",
        location="42 Oak Street, Springfield",
        now=datetime(2026, 7, 12, 8, 0),
    )
    assert text.startswith("BEGIN:VCALENDAR")
    assert "END:VCALENDAR" in text
    assert "BEGIN:VEVENT" in text and "END:VEVENT" in text
    assert "UID:bk-test01@voice-desk-agent" in text
    assert "DTSTART:20260714T090000" in text
    assert "DTEND:20260714T100000" in text
    assert "STATUS:CONFIRMED" in text
    # RFC 5545 requires CRLF line breaks.
    assert "\r\n" in text


def test_render_ics_escapes_special_chars():
    text = render_ics(
        _booking(issue="fix pipe; check valve, urgent"),
        business_name="Miller Plumbing & Heating",
        location="42 Oak Street, Springfield",
    )
    # Commas and semicolons in TEXT values must be backslash-escaped.
    assert "\\;" in text
    assert "\\," in text


async def test_sink_writes_file(tmp_path: Path):
    sink = IcsConfirmationSink(
        outbox_dir=tmp_path,
        business_name="Miller Plumbing & Heating",
        location="42 Oak Street, Springfield",
    )
    path = Path(await sink.send(_booking()))
    assert path.exists()
    assert path.name == "booking-bk-test01.ics"
    assert path.read_text(encoding="utf-8").startswith("BEGIN:VCALENDAR")


async def test_sink_failure_injection(tmp_path: Path):
    sink = IcsConfirmationSink(
        outbox_dir=tmp_path,
        business_name="Miller Plumbing & Heating",
        location="42 Oak Street, Springfield",
        fail_mode="confirmation",
    )
    with pytest.raises(ProviderError):
        await sink.send(_booking())
