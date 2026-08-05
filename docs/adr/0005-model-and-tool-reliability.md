# ADR-0005: gpt-4.1 with sequential tool calls, and fact-only tool outputs

**Status:** accepted · 2026-07-13

## Context

The first working version defaulted to `gpt-4o-mini` for cost. Live eval runs
exposed a serious failure: the model would *narrate* tool results — "let me
check… we have 9 AM available", "you're all booked" — **without emitting the
function call**. It hallucinated availability and confirmations. This is the
exact failure the slot-id anti-hallucination guard exists to prevent, and the
eval harness caught it immediately (3/10 scenarios).

Two more reliability bugs surfaced while fixing this:

- A single booking turn legitimately chains `identify_caller → check_availability
  → book_appointment → send_confirmation` — four tool steps. `AgentSession`
  caps consecutive tool calls per turn at `max_tool_steps=3` by default, so the
  chain was silently truncated and the booking (or its confirmation) dropped.
- Tool return values that embedded imperatives ("now call book_appointment with
  its slot_id", "do not read the ids aloud") were read by the model as
  directives, which drove re-call loops until the step cap was hit.

## Decision

1. **Default to `gpt-4.1` with `parallel_tool_calls=False` and `temperature=0`.**
   This matches LiveKit's own frontdesk example and 2026 guidance that gpt-4.1 is
   the reliable multi-step tool-caller for voice agents. `gpt-4.1-mini` is
   documented as the latency-sensitive alternative via `LLM_MODEL`.
2. **`max_tool_steps=8`** on the session, shared between the runtime agent and
   the eval harness so both exercise the same multi-tool turns.
3. **Tool return values are plain facts, never instructions.** All behavioural
   rules (read-backs, don't-speak-slot-ids, book decisively, graceful
   degradation) live in the system prompt; tools state only what happened.
4. **A 60 s timeout + 5 retries** on the LLM absorb transient API hiccups so a
   network flake is never scored as an agent failure.

## Consequences

- Reliability went from 3/10 (single run) to **59/60 scenario-runs across 5 full
  passes** of the suite as it now stands (10 scenarios then, 12 now). The
  `--runs N` flag makes that reliability, not a single lucky pass, the reported
  number — and the one scenario still moving, `unknown_caller` at 12 of 15
  (4/5 in the full sweep, 8/10 in a follow-up sweep of that scenario alone), is
  named in the README rather than averaged away.
- **The read-back failure was a tool-output defect, not a prompt defect.** The
  lookup result named the number with a pronoun, so a digit-by-digit read-back
  meant recovering it from the transcript and splitting it unaided, several
  steps after the lookup. It names the number now — still a fact, not an
  instruction, which is the rule above. Landed in `50079d6`, which is *later*
  than every commit stamped into `evals/results/`, so the committed 59/60 and
  12/15 measure the agent before it. No post-fix number is quoted anywhere until
  `--runs 5` has been re-run and its artifact committed.
- gpt-4.1 costs more and is slower than mini. Its TTFT p50 is 647.5 ms over 160
  turns (`evals/results/reliability.json`); no committed sweep measures mini, so
  no figure for it is quoted. The README states the tradeoff and the model is one
  env var to change.
- Because tool outputs no longer carry instructions, the prompt is the single
  source of behaviour — easier to reason about and to test.
- **Booking is decisive once consent and details are present.** An earlier prompt
  kept adding "let me confirm the spelling" turns after the caller had already
  said "book it", which the scenario judge scored as an incomplete task. The
  read-backs the prompt does require are enumerated; anything outside that list
  is not a reason to spend another turn.
