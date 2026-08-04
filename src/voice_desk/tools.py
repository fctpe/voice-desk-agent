"""The front-desk agent and its function tools.

The tools are the only way the model can touch the world. Three design choices
matter most:

* **Server-issued slot ids (anti-hallucination).** ``check_availability`` mints
  ids and records them in a per-call cache on the shared :class:`DeskContext`.
  ``book_appointment`` / ``reschedule_appointment`` refuse any id that was not
  handed out during this call, so the model cannot invent a plausible time.
* **One identity per call (authorization).** ``authenticated_phone`` is the
  number this call is authenticated *as*, and ``cancel``/``reschedule`` reach
  into the calendar by number, so they refuse any other one. Saying a number out
  loud is not proof of owning it, so without this a caller could cancel a
  stranger's appointment by reciting it. Nothing the caller can ask for is
  allowed to *set* that number either — a tool that mints the identity hands out
  the authority it guards (ADR-0006).
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
from .prompts import (
    BUSINESS_ADDRESS,
    BUSINESS_NAME,
    OWNER_NAME,
    SYSTEM_PROMPT,
    spell_digits,
    spoken_number,
)
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
    SlotUnavailableError,
)
from .providers.memory import _digits
from .tracing import traced_tool


@dataclass
class DeskContext:
    """Per-call state shared between the session and every tool (``ctx.userdata``)."""

    calendar: CalendarProvider
    contacts: ContactsProvider
    confirmations: ConfirmationSink
    settings: Settings

    # Anti-hallucination cache: slot ids this call has actually offered.
    issued_slots: dict[str, Slot] = field(default_factory=dict)

    # Whose existing appointments this call may reach into, bound once (see
    # _identity_refusal). On a real trunk the agent sets it from SIP caller-ID
    # before the first turn; off-trunk it is the first number the caller states,
    # which is weaker on purpose (ADR-0006).
    authenticated_phone: str | None = None

    # Whether the caller's own word may become that identity. Off-trunk it is
    # the only identity on offer. On a trunk caller-ID is the identity, so a call
    # whose caller-ID never arrived stays unidentified rather than degrading to
    # what the caller says — that degradation is the attack (ADR-0006).
    allow_caller_stated_identity: bool = True

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
    @traced_tool
    async def identify_caller(self, ctx: RunContext[DeskContext], phone: str) -> str:
        """Look up a caller by their phone number to greet returning customers by name.

        Call this once, early, with the caller's own number. That first number is
        the only one whose appointments this call can then change.

        Args:
            phone: The caller's phone number, in any format.
        """
        data = ctx.userdata
        # Off-trunk this is the only identity the call gets. Bind it once: a
        # second number — a caller "correcting" themselves, or one spoken over
        # caller-ID — must never take over the call's identity.
        if data.allow_caller_stated_identity and data.authenticated_phone is None:
            data.authenticated_phone = phone
        # Every branch below names the number it looked up, spelled out. It used
        # to say "that phone number" — a pronoun for a value the tool is holding.
        #
        # That reads as a wording nicety and is not one. The eval scenario that
        # only passed ~80% of the time failed in exactly the runs where the model
        # chained `check_availability` before speaking: with the digits nowhere
        # in the tool output, reading them back means recovering them from the
        # transcript and splitting them itself, and that got dropped the moment
        # anything else came between the number and the reply. The runs that
        # passed are the ones where it answered immediately.
        #
        # So the digits go in the fact. Not an instruction to read them back —
        # tool output stating what the tool did, which is what a lookup result
        # is, and which happens to be the string the caller needs to hear. It
        # also lets the model catch its own transcription error, because the
        # number it passed is now visible rather than assumed.
        spoken = spoken_number(phone)
        try:
            contact = await data.contacts.lookup(phone)
        except ProviderError:
            return (
                f"Customer records are unavailable; no caller record could be loaded for {spoken}."
            )
        if contact is None:
            return f"No customer record exists for {spoken} (new caller)."
        data.caller = contact
        note = f" Note: {contact.notes}." if contact.notes else ""
        return f"Returning customer: {contact.name}, at {spoken}.{note}"

    # -- availability -------------------------------------------------------
    @function_tool
    @traced_tool
    async def check_availability(
        self, ctx: RunContext[DeskContext], day_preference: str, time_window: str
    ) -> str:
        """Find open appointment times matching the caller's preference. Only
        the slots this returns may be booked; their slot_ids are needed for
        book_appointment.

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
            return "The calendar is temporarily unavailable; no times could be retrieved."
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
                return "No open appointment times are available in the next two weeks."
            data.issue(slots)
            return (
                "No times matched that exact request. Soonest available instead:\n"
                f"{_format_options(slots)}"
            )

        data.issue(slots)
        return f"Available appointment times:\n{_format_options(slots)}"

    # -- booking ------------------------------------------------------------
    @function_tool
    @traced_tool
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
            return "That slot id was not among the times offered in this call; it cannot be booked."
        try:
            booking = await data.calendar.book(slot_id=slot_id, name=name, phone=phone, issue=issue)
        except SlotUnavailableError:
            return "That time is no longer available; it may have just been taken."
        except ProviderError:
            return "The calendar is temporarily unavailable; the booking could not be completed."
        data.bookings.append(booking)
        # Deliberately leaves authenticated_phone alone. Booking for someone else
        # (a landlord for a tenant) is normal work, so this number is a claim,
        # not a proof — binding it would let an unidentified caller mint the very
        # identity cancel/reschedule check against (ADR-0006).
        return f"Booked: {booking.slot.spoken()} for {name}, phone {spell_digits(phone)}."

    @function_tool
    @traced_tool
    async def reschedule_appointment(
        self, ctx: RunContext[DeskContext], phone: str, new_slot_id: str
    ) -> str:
        """Move an existing appointment to a different time. Use this (not
        book_appointment) when the caller wants to change the day or time of a
        booking they already have. Requires a fresh slot_id from
        check_availability for the new time.

        Args:
            phone: The phone number the original appointment was booked under.
            new_slot_id: The id of a NEW slot returned by check_availability.
        """
        data = ctx.userdata
        refusal = _identity_refusal(data, phone)
        if refusal:
            return refusal
        if new_slot_id not in data.issued_slots:
            return "That slot id was not among the times offered in this call."
        try:
            booking = await data.calendar.reschedule(phone=phone, new_slot_id=new_slot_id)
        except BookingNotFoundError:
            return f"No existing appointment is on file for {spoken_number(phone)}."
        except ProviderError:
            return "The calendar is temporarily unavailable; the reschedule could not be made."
        return f"Rescheduled to {booking.slot.spoken()}."

    @function_tool
    @traced_tool
    async def cancel_appointment(self, ctx: RunContext[DeskContext], phone: str) -> str:
        """Cancel the caller's existing appointment.

        Args:
            phone: The phone number the appointment was booked under.
        """
        data = ctx.userdata
        refusal = _identity_refusal(data, phone)
        if refusal:
            return refusal
        try:
            booking = await data.calendar.cancel(phone)
        except BookingNotFoundError:
            return f"No active appointment is on file for {spoken_number(phone)}."
        except ProviderError:
            return "The calendar is temporarily unavailable; the cancellation could not be made."
        return f"Cancelled the appointment that was on {booking.slot.spoken()}."

    # -- escalation ---------------------------------------------------------
    @function_tool
    @traced_tool
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
        return f"Message saved for {OWNER_NAME}; he will call back {when}."

    # -- confirmation -------------------------------------------------------
    @function_tool
    @traced_tool
    async def send_confirmation(self, ctx: RunContext[DeskContext], phone: str) -> str:
        """Send a calendar confirmation for a booking made during this call.

        Args:
            phone: The phone number the appointment was booked under.
        """
        data = ctx.userdata
        # Not identity-checked, unlike cancel/reschedule: _latest_booking_for
        # only ever sees bookings this call made itself, so there is no stranger's
        # appointment within reach. A landlord who books for a tenant can confirm
        # it, which is the flow the prompt promises (ADR-0006).
        booking = _latest_booking_for(data, phone)
        if booking is None:
            return f"There is no booking on file for {spoken_number(phone)} to confirm."
        try:
            path = await data.confirmations.send(booking)
        except ProviderError:
            return "The booking is secure, but the confirmation message could not be sent."
        data.confirmations_sent.append(path)
        return "Confirmation sent."


def _format_options(slots: list[Slot]) -> str:
    return "\n".join(
        f"  {i}. {slot.spoken()}  [slot_id: {slot.id}]" for i, slot in enumerate(slots, start=1)
    )


def _identity_refusal(data: DeskContext, phone: str) -> str | None:
    """Why ``phone`` may not be acted on, or None if it is this caller's own.

    Strict phone matching decides *which* booking a number refers to; it says
    nothing about who is allowed to touch it. That is this check, and it guards
    the two tools that reach into the calendar by number. Fails closed — with
    nothing bound there is no one to compare against, so no.

    Nothing bound has two causes with two different ways out, so it says which:
    off-trunk the model simply has not identified the caller yet, but on a trunk
    the caller-ID never arrived and no amount of identifying will help.
    """
    if data.authenticated_phone is None:
        if data.allow_caller_stated_identity:
            return (
                "This call has no identified caller number yet; call identify_caller with the "
                "caller's own number before changing any appointment."
            )
        return (
            "This call has no identified caller number and cannot get one, so no existing "
            "appointment can be changed; offer to take a message instead."
        )
    if _digits(phone) != _digits(data.authenticated_phone):
        return (
            "That number is not this caller's own number; only the appointment booked under "
            "the number this call is identified with can be changed."
        )
    return None


def _latest_booking_for(data: DeskContext, phone: str) -> Booking | None:
    """The caller's most recent active booking, matched strictly by phone.

    No "closest active booking" fallback: confirming a booking the requested
    number never made would send the wrong appointment and falsely report
    success — cancel/reschedule match strictly too, and this must match them.
    """
    key = _digits(phone)
    for booking in reversed(data.bookings):
        if _digits(booking.phone) == key and booking.status != "cancelled":
            return booking
    return None
