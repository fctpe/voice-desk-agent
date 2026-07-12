"""The front-desk agent and its function tools.

The tools are the only way the model can touch the world. Two design choices
matter most:

* **Server-issued slot ids (anti-hallucination).** ``check_availability`` mints
  ids and records them in a per-call cache on the shared :class:`DeskContext`.
  ``book_appointment`` / ``reschedule_appointment`` refuse any id that was not
  handed out during this call, so the model cannot invent a plausible time.
* **Failures are spoken, not raised.** Provider outages come back as calm,
  actionable guidance for the model ("offer to take a message"), never as an
  exception that would drop the call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from livekit.agents import Agent, RunContext, function_tool

from .config import Settings
from .prompts import BUSINESS_ADDRESS, BUSINESS_NAME, OWNER_NAME, SYSTEM_PROMPT, spell_digits
from .providers import (
    Booking,
    BookingNotFoundError,
    CalendarProvider,
    ConfirmationSink,
    Contact,
    ContactsProvider,
    IcsConfirmationSink,
    MemoryCalendar,
    MemoryContacts,
    Message,
    ProviderError,
    Slot,
)


@dataclass
class DeskContext:
    """Per-call state shared between the session and every tool (``ctx.userdata``)."""

    calendar: CalendarProvider
    contacts: ContactsProvider
    confirmations: ConfirmationSink
    settings: Settings

    # Anti-hallucination cache: slot ids this call has actually offered.
    issued_slots: dict[str, Slot] = field(default_factory=dict)

    # Conversation memory / audit trail.
    caller: Contact | None = None
    bookings: list[Booking] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    confirmations_sent: list[str] = field(default_factory=list)

    def issue(self, slots: list[Slot]) -> None:
        for slot in slots:
            self.issued_slots[slot.id] = slot


def build_desk_context(settings: Settings, *, now: datetime | None = None) -> DeskContext:
    """Construct a DeskContext backed by the in-memory providers."""
    calendar = MemoryCalendar(
        seed=settings.demo_seed,
        latency_ms=settings.demo_latency_ms,
        fail_mode=settings.demo_fail,
        now=now,
    )
    contacts = MemoryContacts(
        seed=settings.demo_seed,
        latency_ms=settings.demo_latency_ms,
        fail_mode=settings.demo_fail,
    )
    confirmations = IcsConfirmationSink(
        outbox_dir=settings.outbox_dir,
        business_name=BUSINESS_NAME,
        location=BUSINESS_ADDRESS,
        fail_mode=settings.demo_fail,
    )
    return DeskContext(
        calendar=calendar,
        contacts=contacts,
        confirmations=confirmations,
        settings=settings,
    )


async def _enter_filler(ctx: Any, text: str, delay: float = 0.6) -> Any:
    """Best-effort start of a spoken filler; returns a handle to close later.

    Uses the 1.6 ``RunContext.with_filler`` API when available (real voice
    session). In text-mode evals or direct unit-test calls the context has no
    such method, so this is a no-op — the filler is a nicety, never load-bearing.
    """
    factory = getattr(ctx, "with_filler", None)
    if factory is None:
        return None
    try:
        cm = factory(text, delay=delay)
        await cm.__aenter__()
        return cm
    except Exception:
        return None


async def _exit_filler(cm: Any) -> None:
    if cm is not None:
        try:
            await cm.__aexit__(None, None, None)
        except Exception:
            pass


class FrontDeskAgent(Agent):
    """Riley — the AI front desk for Miller Plumbing & Heating."""

    def __init__(self) -> None:
        super().__init__(instructions=SYSTEM_PROMPT)

    # -- identification -----------------------------------------------------
    @function_tool
    async def identify_caller(self, ctx: RunContext[DeskContext], phone: str) -> str:
        """Look up a caller by their phone number to greet returning customers by name.

        Call this once, early, if the caller provides a phone number.

        Args:
            phone: The caller's phone number, in any format.
        """
        data = ctx.userdata
        try:
            contact = await data.contacts.lookup(phone)
        except ProviderError:
            return (
                "The customer records are unavailable right now. Proceed as if this is a "
                "new caller and collect their details normally."
            )
        if contact is None:
            return (
                "No existing record for that number. Treat this as a new caller: "
                "ask for their name and confirm the phone number by reading it back."
            )
        data.caller = contact
        note = f" Note: {contact.notes}." if contact.notes else ""
        return (
            f"Returning customer found: {contact.name}.{note} "
            f"Greet them warmly by name and continue."
        )

    # -- availability -------------------------------------------------------
    @function_tool
    async def check_availability(
        self, ctx: RunContext[DeskContext], day_preference: str, time_window: str
    ) -> str:
        """Find open appointment times matching the caller's preference.

        Say a brief filler ("Let me check the calendar") before calling this.
        Only the slots this returns may be booked.

        Args:
            day_preference: The caller's day, e.g. "tomorrow", "next Tuesday",
                "Friday", or "as soon as possible".
            time_window: Preferred part of the day: "morning", "afternoon",
                "evening", or "any".
        """
        data = ctx.userdata
        filler = await _enter_filler(ctx, "Let me check the calendar for you.")
        try:
            slots = await data.calendar.find_slots(
                day_preference=day_preference, time_window=time_window, limit=3
            )
        except ProviderError:
            return (
                "The calendar is temporarily unavailable. Apologise briefly and offer to "
                "take a message so the office can call back with times — use take_message."
            )
        finally:
            await _exit_filler(filler)

        if not slots:
            # Fall back to the soonest options on any day so the caller isn't stuck.
            try:
                slots = await data.calendar.find_slots(
                    day_preference="", time_window=time_window, limit=3
                )
            except ProviderError:
                slots = []
            if not slots:
                return (
                    "No open times are available in the next two weeks for that request. "
                    "Apologise and offer to take a message with take_message so the office "
                    "can arrange something."
                )
            data.issue(slots)
            options = _format_options(slots)
            return (
                "Nothing matched that exact request, but here are the soonest available "
                f"alternatives. Offer them by day and time only:\n{options}\n"
                "Do not read the slot ids aloud. When the caller picks one, call "
                "book_appointment with its slot_id."
            )

        data.issue(slots)
        options = _format_options(slots)
        return (
            "Available times (offer these to the caller by day and time only, do not read "
            f"the slot ids aloud):\n{options}\n"
            "When the caller chooses one, call book_appointment with its slot_id."
        )

    # -- booking ------------------------------------------------------------
    @function_tool
    async def book_appointment(
        self,
        ctx: RunContext[DeskContext],
        slot_id: str,
        name: str,
        phone: str,
        issue: str,
    ) -> str:
        """Book one of the previously offered appointment slots.

        Args:
            slot_id: The id of a slot returned by check_availability in this call.
            name: The caller's full name.
            phone: The caller's callback phone number.
            issue: The reason for the visit (e.g. "leaking radiator").
        """
        data = ctx.userdata
        if slot_id not in data.issued_slots:
            return (
                "That slot id was not one of the options offered in this call, so it cannot "
                "be booked. Call check_availability and choose one of the times it returns "
                "before booking. Never invent a time."
            )
        try:
            booking = await data.calendar.book(slot_id=slot_id, name=name, phone=phone, issue=issue)
        except ProviderError:
            return (
                "That time was just taken or the calendar hiccuped. Apologise, call "
                "check_availability again, and offer fresh options."
            )
        data.bookings.append(booking)
        digits = spell_digits(phone)
        return (
            f"Booked: {booking.slot.spoken()} for {name}. "
            f"Confirm it back to the caller as a concrete date and time, read the phone "
            f"number back digit by digit ({digits}), then call send_confirmation."
        )

    @function_tool
    async def reschedule_appointment(
        self, ctx: RunContext[DeskContext], phone: str, new_slot_id: str
    ) -> str:
        """Move an existing appointment to a different offered slot.

        Args:
            phone: The phone number the original appointment was booked under.
            new_slot_id: The id of a NEW slot returned by check_availability.
        """
        data = ctx.userdata
        if new_slot_id not in data.issued_slots:
            return (
                "That new slot id was not offered in this call. Call check_availability "
                "first and pick one of the returned times."
            )
        try:
            booking = await data.calendar.reschedule(phone=phone, new_slot_id=new_slot_id)
        except BookingNotFoundError:
            return (
                "No existing appointment is on file for that number. Offer to book a new "
                "one instead, or take a message."
            )
        except ProviderError:
            return (
                "The calendar is temporarily unavailable. Apologise and offer to take a "
                "message with take_message."
            )
        return (
            f"Rescheduled to {booking.slot.spoken()}. Confirm the new date and time back to "
            f"the caller, then call send_confirmation."
        )

    @function_tool
    async def cancel_appointment(self, ctx: RunContext[DeskContext], phone: str) -> str:
        """Cancel the caller's existing appointment.

        Args:
            phone: The phone number the appointment was booked under.
        """
        data = ctx.userdata
        try:
            booking = await data.calendar.cancel(phone)
        except BookingNotFoundError:
            return (
                "No active appointment is on file for that number. Let the caller know, and "
                "offer to book one or take a message."
            )
        except ProviderError:
            return (
                "The calendar is temporarily unavailable. Apologise and offer to take a "
                "message with take_message."
            )
        return (
            f"Cancelled the appointment that was on {booking.slot.spoken()}. Confirm the "
            "cancellation to the caller and ask if there's anything else."
        )

    # -- escalation ---------------------------------------------------------
    @function_tool
    async def take_message(
        self,
        ctx: RunContext[DeskContext],
        name: str,
        phone: str,
        message: str,
        urgent: bool = False,
    ) -> str:
        """Take a message for the owner and promise a call back (human escalation).

        Use this for emergencies (burst pipe, gas smell, no heat) and any request
        you cannot handle yourself, such as pricing or quotes.

        Args:
            name: The caller's name.
            phone: The caller's callback number.
            message: A concise summary of what the caller needs.
            urgent: True if this is an emergency needing an immediate call back.
        """
        data = ctx.userdata
        data.messages.append(Message(name=name, phone=phone, body=message, urgent=urgent))
        when = "right away" if urgent else "as soon as possible"
        return (
            f"Message saved for {OWNER_NAME}. Tell the caller you'll pass this to "
            f"{OWNER_NAME} {when} and that he'll call them back on the number you confirmed."
        )

    # -- confirmation -------------------------------------------------------
    @function_tool
    async def send_confirmation(self, ctx: RunContext[DeskContext], phone: str) -> str:
        """Send a calendar confirmation for the caller's most recent booking.

        Args:
            phone: The phone number the appointment was booked under.
        """
        data = ctx.userdata
        booking = _latest_booking_for(data, phone)
        if booking is None:
            return (
                "There is no booking to confirm yet. Book the appointment first with "
                "book_appointment."
            )
        try:
            path = await data.confirmations.send(booking)
        except ProviderError:
            return (
                "The appointment is booked, but the confirmation could not be sent right "
                "now. Reassure the caller that the booking itself is secure."
            )
        data.confirmations_sent.append(path)
        return "Confirmation sent. Let the caller know it's on its way and wrap up warmly."


def _format_options(slots: list[Slot]) -> str:
    return "\n".join(
        f"  {i}. {slot.spoken()}  [slot_id: {slot.id}]" for i, slot in enumerate(slots, start=1)
    )


def _latest_booking_for(data: DeskContext, phone: str) -> Booking | None:
    from .providers.memory import _digits

    key = _digits(phone)
    for booking in reversed(data.bookings):
        if _digits(booking.phone) == key and booking.status != "cancelled":
            return booking
    # Fall back to the most recent active booking of the call.
    for booking in reversed(data.bookings):
        if booking.status != "cancelled":
            return booking
    return None
