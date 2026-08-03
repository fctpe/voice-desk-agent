# Security

## Caller identity

- A call has exactly one identity, bound once, and **no tool the caller can ask for may set it** (`DeskContext.authenticated_phone`, [ADR-0006](docs/adr/0006-caller-identity-binding.md)). Booking does not mint identity either: an unidentified caller who could book a throwaway slot under someone else's number and be handed that number as their identity would have talked their way past the guard rather than through it.
- **On a trunk, the identity is caller-ID.** With `SIP_TRUNK=1` the entrypoint waits for the SIP participant (`ctx.wait_for_participant(kind=[…PARTICIPANT_KIND_SIP])`, 5 s) and reads `sip.phoneNumber` off it (`src/voice_desk/agent.py`). Reading `room.remote_participants` directly is a race — the list syncs asynchronously after connect — so on the unlucky ordering a trunk call would silently become a self-asserted one.
- **A trunk that yields no caller-ID fails closed.** No SIP participant inside the timeout, or one that withholds the attribute, leaves the call unidentified: it can book and leave messages, and it can change nothing. That is logged at error level, naming which of the two happened; running without a trunk is logged at info, so "console" and "trunk whose caller-ID never arrived" are never the same line.
- **Off a trunk the identity is only as strong as the caller's first claim.** With no caller-ID, the identity is the first number stated to `identify_caller` and nothing else. Bind-once stops a caller *switching* identity mid-call; it does nothing about one who states the victim's number from the first sentence. Only real caller-ID fixes that, and only a real trunk supplies caller-ID. The console demo has no trunk.
- `identify_caller` will still look up any number, so the contacts book remains a name-and-notes oracle for someone dialling through numbers. It changes no state; closing it needs a policy about what a front desk may say out loud, which is a separate decision.

## Tool guards

- **`cancel_appointment` and `reschedule_appointment` refuse any number but the call's own**, and refuse outright when nothing is bound — there is no one to compare against, so the answer is no. Refusals come back as spoken plain facts, never exceptions, and they name the way out, which differs by mode: off-trunk, identify the caller first; on a trunk with no caller-ID, nothing will identify them, so offer to take a message.
- **`book_appointment`, `take_message` and `send_confirmation` are deliberately open.** The first two create new state under a claimed number rather than reaching into someone else's, which is ordinary front-desk work (a landlord booking for a tenant). `send_confirmation` scans `DeskContext.bookings` — bookings *this call made* — so there is no stranger's appointment within its reach.
- **Slot ids are server-issued.** `check_availability` mints them into a per-call cache; `book_appointment` and `reschedule_appointment` refuse any id not handed out during this call (`src/voice_desk/tools.py`). Without it a model can invent a plausible-looking time and narrate a booking that never happened — which is the failure this repo refuses everywhere else.
- Phone matching in `_latest_booking_for` and `MemoryCalendar.find_booking` is strict, with no closest-active-booking fallback. It decides *which* booking a number refers to and says nothing about who may touch it; both checks are needed and they are not the same check.

## Data and credentials

- **The calendar and contacts are fakes** — deterministic, in-memory, no network ([ADR-0001](docs/adr/0001-fake-providers.md)). There is no real customer record in this repo to leak. Swapping in production adapters behind the same `Protocol` ports changes that, and the guards above are what would then be load-bearing.
- **Transcripts are verbatim and unredacted.** `transcripts/transcript-<call_id>.jsonl` gets one line per conversation item as the call unfolds, including every name, number and address the caller says out loud. `.ics` confirmations in `outbox/` embed the booking's name and phone in the event description. Both directories, plus `metrics/`, are gitignored — treat them as call recordings, not as build artifacts.
- API keys are read from the environment into a frozen `Settings` and never logged; `missing_keys()` reports variable *names* only, so a keyless run prints guidance rather than a value. `.env` is gitignored; only `.env.example` is committed, and every credential line in it is empty.
- Tracing is off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set — no endpoint, no provider, no exporter ([ADR-0007](docs/adr/0007-opentelemetry-genai.md)). This project's own span attributes are the operation name, tool name and speech id (`src/voice_desk/tracing.py`) — no arguments, no transcript. The surrounding spans belong to `livekit-agents`, not to this repo, so treat a collector endpoint as in scope for the call's contents rather than assuming it is not.
- The unit suite runs fully offline: an autouse fixture fails any test that opens a socket, and `tests/test_network_guard.py` asserts the guard itself still blocks.

## Reporting

Open a GitHub security advisory or email the address on the profile of [@fctpe](https://github.com/fctpe). Please do not open public issues for vulnerabilities.
