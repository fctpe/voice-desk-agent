"""Offline iCalendar (RFC 5545) confirmation writer.

Renders a booking to a ``VEVENT`` and writes it to the outbox as a ``.ics``
file. There is deliberately no network here: in a real deployment this sink
would be swapped for one that emails or texts the invite, but for a portfolio
demo a file on disk is inspectable and reproducible.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .base import Booking

_PRODID = "-//voice-desk-agent//Riley Front Desk//EN"


def _escape(text: str) -> str:
    """Escape TEXT values per RFC 5545 section 3.3.11."""
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fmt(dt: datetime) -> str:
    """Local floating time (no TZID) — keeps the demo free of tz-database deps."""
    return dt.strftime("%Y%m%dT%H%M%S")


def render_ics(
    booking: Booking,
    *,
    business_name: str,
    location: str,
    now: datetime | None = None,
) -> str:
    """Return the full VCALENDAR text for a booking."""
    stamp = _fmt(now or datetime.now())
    summary = f"{business_name} — {booking.issue}" if booking.issue else business_name
    description = (
        f"Appointment for {booking.name} ({booking.phone}). "
        f"Reason: {booking.issue or 'not specified'}. "
        f"Booked via Riley, the AI front desk."
    )
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{booking.id}@voice-desk-agent",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{_fmt(booking.slot.start)}",
        f"DTEND:{_fmt(booking.slot.end)}",
        f"SUMMARY:{_escape(summary)}",
        f"LOCATION:{_escape(location)}",
        f"DESCRIPTION:{_escape(description)}",
        "STATUS:CONFIRMED",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    # RFC 5545 mandates CRLF line breaks.
    return "\r\n".join(lines) + "\r\n"


class IcsConfirmationSink:
    """A :class:`voice_desk.providers.base.ConfirmationSink` that writes ``.ics`` files."""

    def __init__(
        self,
        *,
        outbox_dir: str | Path,
        business_name: str,
        location: str,
        fail_mode: str = "none",
    ) -> None:
        self._outbox = Path(outbox_dir)
        self._business_name = business_name
        self._location = location
        self._fail_mode = fail_mode

    async def send(self, booking: Booking) -> str:
        if self._fail_mode == "confirmation":
            from .base import ProviderError

            raise ProviderError("confirmation sink unavailable")

        self._outbox.mkdir(parents=True, exist_ok=True)
        path = self._outbox / f"booking-{booking.id}.ics"
        path.write_text(
            render_ics(
                booking,
                business_name=self._business_name,
                location=self._location,
            ),
            encoding="utf-8",
        )
        return str(path)
