# voice-desk-agent

**Riley** — an AI voice front desk for field-service businesses, built on
[LiveKit Agents](https://github.com/livekit/agents). Riley answers the phone for
a (fictional) plumbing and heating company, books and reschedules appointments,
takes messages, and escalates emergencies — all over a real-time voice pipeline
you can try in your terminal with a single API key.

> Field-service businesses live and die by the phone. A plumber under a sink or
> a technician on a roof can't answer, and **a missed call is usually a lost
> job** — the caller simply dials the next company. An always-available front
> desk that can actually *book the work* recovers that revenue. That is the
> problem Riley is built to solve.

This is a portfolio project: the business, address, and phone number are
invented, and the calendar is a deterministic in-memory fake **by design** (see
[ADR-0001](docs/adr/0001-fake-providers.md)) so the whole thing runs offline and
reproducibly.

---

## Architecture

```mermaid
flowchart LR
    Caller([Caller]) -->|audio| Pipeline
    subgraph Pipeline["LiveKit AgentSession"]
        STT[STT<br/>Deepgram / OpenAI] --> LLM[LLM<br/>gpt-4o-mini]
        LLM --> TTS[TTS<br/>Cartesia / OpenAI]
        VAD[Silero VAD<br/>turn-taking] -.-> LLM
    end
    LLM <-->|function tools| Tools
    subgraph Tools["Riley's tools"]
        Cal[(Calendar<br/>fake, seeded)]
        Con[(Contacts<br/>fake, seeded)]
        Ics[.ics outbox]
    end
    Pipeline --> Metrics[Per-turn latency<br/>+ JSONL transcript]
```

The pipeline is **provider-agnostic** and chosen from the environment. Two
presets:

- **`openai`** (default): OpenAI for STT + LLM + TTS — a single `OPENAI_API_KEY`
  runs everything. Zero friction.
- **`full`**: Deepgram STT (nova-3) + OpenAI LLM + Cartesia TTS (sonic-3) — lower
  latency and per-stage cost visibility ([ADR-0002](docs/adr/0002-pipeline-over-speech-to-speech.md)).

---

## Quickstart (console, one key)

You need [uv](https://docs.astral.sh/uv/) and an `OPENAI_API_KEY`.

```bash
uv sync                              # create the environment
cp .env.example .env                 # then put your OPENAI_API_KEY in .env
uv run python -m voice_desk console  # talk to Riley in your terminal
```

That's it — the default `openai` stack needs nothing else. Speak, and Riley
answers. Without a key, the console exits immediately with a one-line hint
instead of a traceback.

Prefer the lower-latency pipeline? Set `VOICE_DESK_STACK=full` and add
`DEEPGRAM_API_KEY` + `CARTESIA_API_KEY`.

Common commands are wrapped in the `Makefile` (`make console`, `make test`,
`make eval`, `make lint`).

---

## Tools

Riley's only way to affect the world is through typed function tools. The
booking tools accept **only slot ids issued earlier in the same call**, which is
the primary guard against a model inventing a time.

| Tool | Purpose |
| --- | --- |
| `identify_caller(phone)` | Look up a returning customer by number to greet them by name. |
| `check_availability(day_preference, time_window)` | Find real open slots and **issue** their ids for this call. |
| `book_appointment(slot_id, name, phone, issue)` | Book an **issued** slot; rejects unknown/invented ids. |
| `reschedule_appointment(phone, new_slot_id)` | Move an existing booking to another issued slot. |
| `cancel_appointment(phone)` | Cancel the caller's appointment. |
| `take_message(name, phone, message, urgent)` | Human escalation — "I'll pass this to Mr. Miller right away." |
| `send_confirmation(phone)` | Write an `.ics` confirmation to the outbox. |

---

## Conversation correctness

The behaviours that make a voice front desk trustworthy are hand-designed in the
prompt and enforced by the tools + evals:

- **No invented slots.** `check_availability` mints server-side slot ids;
  booking refuses anything it did not hand out this call.
- **Read-backs.** Phone numbers are read back digit by digit; uncertain names
  are spelled letter by letter; bookings are confirmed as a concrete date/time.
- **Relative dates are made concrete.** "next Tuesday" is confirmed as an actual
  calendar date, never left ambiguous.
- **Emergencies escalate.** Burst pipe, flooding, or a gas smell skips normal
  booking, takes an urgent message, and promises an immediate call back.
- **Stays in scope, resists injection.** Off-topic and abusive turns are
  deflected politely; attempts to override the rules or demand free work are
  refused (pricing goes to a message for the owner).
- **Degrades gracefully.** Provider outages become a calm spoken apology and an
  offer to take a message — never a dropped call.

---

## Evals

Ten multi-turn scenarios drive the **real** agent and tools over text using
LiveKit's native testing API (`AgentSession.run` + `result.expect...` +
LLM-`judge`), with the deterministic fakes underneath
([ADR-0003](docs/adr/0003-native-text-evals.md)). Each scenario asserts the
expected tool calls and judges the final reply's intent with `gpt-4o-mini`.

```bash
uv run python evals/run_evals.py --list    # the 10 scenarios, zero LLM calls
uv run python evals/run_evals.py --smoke   # first 3 (needs OPENAI_API_KEY)
uv run python evals/run_evals.py           # full run -> evals/results/evals.json
```

Scenarios: happy booking · relative-date ("next Tuesday") · no-slot + alternative
· caller changes mind · reschedule · cancel · unknown caller (capture + read-back)
· off-topic/abuse deflection · prompt-injection attempt · emergency escalation.

<!-- EVAL_RESULTS -->

---

## Latency

For a call to feel human, the reply has to start fast. The comfortable
turn-taking window in human conversation is roughly **500–800 ms** from the
moment you stop talking to the first sound of the response; much beyond that and
the call feels laggy. Riley measures the budget that governs this:

```
EOU -> first audio  =  end_of_utterance_delay + LLM TTFT + TTS TTFB
```

Each stage comes from LiveKit's `metrics_collected` events, correlated per turn
by `speech_id`. At the end of a call Riley prints a p50/p95 line and writes a
JSON report to the metrics directory (`metrics.py`). Using the `full` stack
(Deepgram + Cartesia streaming) generally lands closer to the target than the
OpenAI-only stack.

<!-- LATENCY_RESULTS -->

---

## Limitations (by design and honest)

- **The calendar and contacts are fake.** Deterministic in-memory fakes, chosen
  deliberately for reproducible evals and offline runs
  ([ADR-0001](docs/adr/0001-fake-providers.md)). Production adapters would
  satisfy the same `Protocol` ports without touching the agent.
- **No telephony bundled.** The demo is console-only. Attaching a real phone
  number via LiveKit SIP + Twilio/Telnyx is sketched in
  [docs/telephony.md](docs/telephony.md) but intentionally not wired.
- **English-only (v1).** Positioned for a global English audience; multilingual
  is a roadmap item with its own eval suite, not an untested bolt-on
  ([ADR-0004](docs/adr/0004-english-only-v1.md)).
- **Turn detection defaults to VAD.** The 1.6 audio-native Turn Detector
  (`TURN_DETECTION=model`) is supported but needs LiveKit inference credentials;
  the offline default is Silero VAD endpointing.

---

## Development

```bash
make check     # ruff check + ruff format --check + pytest (all offline)
```

The unit suite runs fully offline; an autouse fixture fails any test that opens
a network socket.

## A note on AI usage

AI-assisted scaffolding was used to accelerate the boilerplate. The
**architecture, the system prompt, the tool contracts, and the eval scenarios
were hand-designed** — the parts that determine whether the agent is actually
correct and trustworthy.

## License

MIT — see [LICENSE](LICENSE).
