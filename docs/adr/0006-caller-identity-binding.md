# ADR-0006: One bound caller identity per call

**Status:** accepted · 2026-08-03

## Context

`cancel_appointment(phone)`, `reschedule_appointment(phone, new_slot_id)` and
`send_confirmation(phone)` took the phone number as a tool argument and verified
nothing about it. Anyone who recited a number could cancel or move that person's
appointment, and the agent would cheerfully report success. On a phone line this
is the easiest attack there is: numbers are read aloud in every call.

The strict phone matching in `_latest_booking_for` and `MemoryCalendar.find_booking`
is not a defence. It decides *which* booking a number refers to; it says nothing
about who is allowed to touch it. Both are needed, and they are different checks.

Real telephony already carries an identity the caller does not choose: the
calling number, delivered by the trunk. LiveKit SIP publishes it on the SIP
participant as the `sip.phoneNumber` attribute (docs/telephony.md). The console
demo has no trunk and therefore no such number.

## Decision

**Identity is what the call is authenticated *as*, and only two things can
supply it: the trunk, or — where there is no trunk — the caller's first claim.
No tool the caller can ask for may set it.** An earlier draft of this ADR let
`book_appointment` bind an unidentified call so the booker could manage what they
had just booked. That was an authority-granting operation dressed as a
convenience: booking is not identity-checked *by design*, so an unidentified
caller could book a throwaway slot under a victim's number, be handed that
number as their identity, and then cancel the victim's real appointment. The two
halves cancelled out. Identity now has one source per call and creating state
never mints it.

1. **`DeskContext.authenticated_phone` — bound once per call, never by a tool.**
   Once set, no later number replaces it. A "correction" and a takeover attempt
   are the same event as far as the tools can tell, so the first binding wins.
2. **On a trunk it comes from caller-ID.** After `ctx.connect()`, `entrypoint`
   *waits* for the SIP participant with
   `ctx.wait_for_participant(kind=[PARTICIPANT_KIND_SIP])` and reads
   `sip.phoneNumber` off it. Reading `room.remote_participants` directly is a
   race: the participant list syncs asynchronously after connect, so on the
   unlucky ordering a trunk call would find no caller-ID and quietly become a
   self-asserted one.
3. **A trunk that yields no caller-ID fails closed.** `SIP_TRUNK=true` says this
   worker answers calls over a trunk; then `DeskContext.allow_caller_stated_identity`
   is False and `identify_caller` cannot bind anything. No SIP participant within
   `_SIP_PARTICIPANT_TIMEOUT` (5 s), or one that withholds `sip.phoneNumber`,
   leaves the call unidentified — able to book and to leave messages, unable to
   change anything — and logs an **error** naming which of the two happened. With
   `SIP_TRUNK` off there is no trunk to fail: that is logged too, at info, so
   "console" and "trunk whose caller-ID never arrived" are never the same line.
4. **Off-trunk it is self-asserted, and that is a weaker mode.** With no
   caller-ID the identity is the first number the caller states to
   `identify_caller`, and nothing else.
5. **`cancel` and `reschedule` refuse any other number**, as a spoken plain fact
   ("that number is not this caller's own number"), never an exception. With
   nothing bound at all they refuse as well — there is no one to compare against,
   so the answer is no — and the refusal names the way out, because there are two
   and they differ: off-trunk, identify the caller first; on a trunk with no
   caller-ID, nothing will identify them, so offer to take a message.
6. **`book_appointment`, `take_message` and `send_confirmation` stay open.** The
   first two create new state under a claimed number rather than reaching into
   someone else's; booking or taking a message for another person (a landlord for
   a tenant) is ordinary front-desk work. `send_confirmation` is open because
   `_latest_booking_for` scans `DeskContext.bookings` — bookings *this call made*
   — so there is no stranger's appointment within its reach. Guarding it bought
   no security and broke the landlord flow the prompt promises in step 5, which
   is exactly the false-success setup this repo refuses elsewhere.

## Consequences

- The cross-caller hole is closed on the trunk path: with caller-ID bound, no
  number the caller says can move the call's identity, so the mutating tools are
  restricted to the line that actually called.
- **The console path is still only as strong as the caller's first claim.**
  Bind-once stops a caller *switching* identity mid-call; it does nothing about
  someone who states the victim's number from the first sentence. Only caller-ID
  fixes that, and only a real trunk supplies caller-ID. Stated in the README
  Limitations rather than papered over.
- **A caller who books must identify themselves before they can change that
  booking**, even within the same call — booking no longer stands in for
  identification. The prompt says so under CHANGING AN EXISTING APPOINTMENT. The
  cost is one extra `identify_caller`; the alternative was the escalation above.
- A caller whose number was misheard and bound wrong cannot manage that booking
  later in the same call — they have to call back. That is the price of refusing
  to rebind, and it is the safe side to fail on.
- Not covered: `identify_caller` will still look up any number, so the contacts
  book remains a name-and-notes oracle for someone dialling through numbers. It
  changes no state, and closing it needs a policy about what a front desk may say
  out loud — a separate decision.
- Covered by `tests/test_tools.py` (refusals, bind-once, fail-closed, and the
  booking-grants-no-identity escalation itself), `tests/test_caller_id.py` (the
  late-joining SIP participant, and both fail-closed paths, against a stubbed
  job context — no trunk is wired in this repo) and two eval scenarios,
  `cross_caller_cancel` and `cross_caller_reschedule`.
- The committed `50 / 50` reliability result was produced at commit `7091520`
  with ten scenarios and prompt hash `25626d2cf31681b9`. This ADR adds two
  scenarios and a prompt rule, so that number does not describe the current tree:
  re-measure with `uv run python evals/run_evals.py --runs 5` before quoting it.
- **Scoping the rule took three sweeps, and the two it rejected are the argument
  for `--runs 5`.** Adding the identity rule at all dropped `happy_booking` to
  3/5: told to call `identify_caller` first when *changing* an appointment, the
  model generalised it to new bookings and spent a turn asking for a number
  instead of checking availability. Scoping the rule fixed that and broke
  something else — "booking needs no identification first" read as licence to
  skip the read-back, and `unknown_caller` fell to 1/5, for a net 56/60.
  Constraining the rule to *ordering* alone is what holds; the suite reads 60/60
  since the read-back fix (ADR-0005). A single
  green run would have shipped either intermediate state.
