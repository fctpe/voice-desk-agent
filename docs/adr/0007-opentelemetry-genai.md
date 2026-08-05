# ADR-0007: Off `metrics_collected`, and opt-in OpenTelemetry

**Status:** accepted · 2026-08-03

## Context

livekit-agents 1.6.5 deprecated the event this project's metrics were built on.
Every eval run printed:

> `metrics_collected is deprecated. Use session_usage_updated for usage tracking
> and ChatMessage.metrics for per-turn latency.`

Reading the 1.6.5 source rather than guessing at the replacements turned up four
things that decide this ADR:

1. **`ChatMessage.metrics`** (delivered on `conversation_item_added`) carries
   `end_of_turn_delay` / `transcription_delay` on the *user* message and
   `llm_node_ttft` / `tts_node_ttfb` / `e2e_latency` / `llm_metadata` on the
   *assistant* message. `e2e_latency` is the EOU→first-audio budget, measured by
   the SDK — including the tool round-trips the old three-stage sum silently
   omitted.
2. **`session_usage_updated`** carries `AgentSessionUsage`: token and audio
   totals per (provider, model). It is a **running session total, not a delta**,
   and it fires after every metric of any kind.
3. **Neither replacement exposes `speech_id`.** One is keyed by message id, the
   other by model. Per-turn token attribution is not available from them.
4. **LiveKit already emits the spans.** `LLMStream` opens an `llm_request` span
   per model call carrying `gen_ai.operation.name`, `gen_ai.request.model`,
   `gen_ai.provider.name`, `gen_ai.usage.input_tokens` and
   `gen_ai.usage.output_tokens`; `AgentActivity` opens `agent_turn` tagged
   `lk.speech_id`; `generation.py` opens `function_tool` per tool call. All of it
   goes to a no-op tracer until a provider is installed.

The GenAI semantic conventions are not settled. In the installed
`opentelemetry-semantic-conventions` the `gen_ai.*` registry ships only under
`opentelemetry.semconv._incubating`, and the package itself has no 1.x release:

```bash
uv run python -c "import importlib.metadata as m; \
print(m.version('opentelemetry-semantic-conventions'))"          # 0.60b1
uv run python -c "from opentelemetry.semconv._incubating.attributes \
import gen_ai_attributes as g; print(g.GEN_AI_TOOL_NAME)"        # gen_ai.tool.name
```

As of July 2026 the conventions are still **Development** status: the
`gen_ai.client` (model-call) spans are stable enough to build on, the
`gen_ai.agent` spans are experimental, and the dedicated conventions repository
has no tagged release to pin a dependency against. LiveKit reaches the same
conclusion — its `telemetry/trace_types.py` spells the attribute strings as
literals rather than importing the semconv package.

## Decision

1. **`metrics.py` reads the two documented replacements**, and reports what each
   honestly is: one row per chat message that has metrics (never a fabricated
   user↔assistant pairing — `e2e_latency` is LiveKit's own pairing and is used as
   such), plus one usage snapshot per call. The snapshot **replaces**, never
   accumulates; summing a running total would multiply the bill by the number of
   events.
2. **Tokens land in the report.** `metrics-<call>.json` grows a `usage` block and
   the end-of-call line prints LLM tokens in/out, so cost per call is measurable
   instead of estimated.
3. **Tracing is opt-in on `OTEL_EXPORTER_OTLP_ENDPOINT`.** Unset means off:
   no exporter is built and no tracer provider is installed, so LiveKit's spans
   resolve to the API's `NonRecordingSpan` and go nowhere. Nothing about running
   the agent requires a collector. What "off" does **not** mean is that the OTel
   machinery is absent — `livekit.agents.telemetry.traces` imports the SDK and
   the OTLP exporters at module scope, so importing this agent loads them
   whatever the variable says, and `voice_desk.tracing` imports them at module
   scope too rather than pretending a lazy import avoids the cost:

   ```bash
   uv run python -c "import sys, voice_desk.agent; \
   print(len([m for m in sys.modules if m.startswith('opentelemetry.exporter.otlp')]))"  # 17
   uv run python -c "import sys, voice_desk.agent; \
   print(len([m for m in sys.modules if m.startswith('opentelemetry')]))"                # 116
   ```
4. **We install a provider; we do not re-emit LiveKit's spans.** A second,
   parallel span tree over the same call would be duplicated work reporting
   duplicated numbers. `voice_desk.tracing.traced_tool` adds the one thing
   LiveKit leaves out — `gen_ai.operation.name` / `gen_ai.tool.name`, plus
   `lk.speech_id` — to LiveKit's own `function_tool` span.
5. **We do not stamp `gen_ai.agent.*` or `gen_ai.operation.name` onto
   `agent_turn`.** That would mean reaching into a third-party span by matching
   its name, to write attributes from the part of the conventions that is
   explicitly experimental. The turn span is exported as LiveKit emits it, with
   `lk.speech_id` as the correlation key.

## Consequences

- The deprecation warning is gone, and `tests/test_tracing.py` fails if it comes
  back — a scripted fake LLM drives the real agent and tools through a
  tool-calling turn offline and asserts the log is clean.
- That same test asserts the `agent_turn` / `llm_request` / `function_tool` spans
  and their GenAI attributes. Decision 4 depends on third-party behaviour, so it
  is pinned by a test rather than trusted.
- **`llm_ttft_ms` changed meaning, and the committed latency number was re-measured
  under the new one.** It used to be the first model call of a turn; it is now the
  call that produced the turn's reply text, because that is the one
  `ChatMessage.metrics` reports. A booking turn makes four tool calls, so these
  are different numbers and figures from before the change are not comparable.
  `evals/results/reliability.json` now holds a `--runs 5` sweep taken under the
  new definition — p50 790.6 ms, p95 2066.3 ms over 160 turns — so the warning
  that used to sit here is closed.
- `response_ms` is now `e2e_latency` from the SDK rather than
  `eou + ttft + ttfb` summed here. It is the better number — it includes tool
  execution — but it only exists on a real voice call, so text-mode evals report
  `null` for it, as they always effectively did.
- The stage columns come from different messages now (`eou_delay_ms` from user
  rows, `llm_ttft_ms` from assistant rows), so `metrics-<call>.json` has roughly
  twice the rows for the same call. `summary.turns` counts assistant messages.
- OTel is declared as a direct dependency because `voice_desk.tracing` imports
  it. This does not change what gets installed, per the counts above.
- `tests/test_tracing.py::test_what_disabled_actually_means` pins both halves of
  decision 3 — a non-recording span and no installed provider, but the OTLP
  exporter present in `sys.modules` — so the disabled path is measured rather
  than described.
- Not covered: nothing in this repo exercises a real collector. The exporter is
  the stock `OTLPSpanExporter`, configured entirely from the standard
  `OTEL_EXPORTER_OTLP_*` environment, and it is the one link in the chain the
  offline test suite cannot prove.
