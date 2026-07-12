# ADR-0002: STT-LLM-TTS pipeline over a speech-to-speech model

## Status
Accepted.

## Context
LiveKit Agents supports two architectures: a cascaded **STT → LLM → TTS**
pipeline, or a single **speech-to-speech** realtime model (e.g. a realtime
audio LLM). Speech-to-speech can feel more natural and can be lower latency, but
it collapses the pipeline into one opaque call.

## Decision
Use the cascaded pipeline.

## Consequences
- **Per-stage metrics.** We can measure EOU delay, LLM TTFT, and TTS TTFB
  independently and compute an `EOU -> first audio` budget (see `metrics.py`).
  With speech-to-speech those stages are not separable.
- **Per-stage cost and provider choice.** Each stage is swappable via env
  (`VOICE_DESK_STACK`), so you can trade cost against latency (OpenAI-only for
  zero-friction, or Deepgram + Cartesia for a snappier pipeline) without
  touching agent code.
- **Text-mode evals.** Because the LLM turn is a discrete text step, the evals
  can drive the agent over text with no audio at all — the key enabler for
  ADR-0003.
- **Trade-off accepted.** A well-tuned speech-to-speech model may feel more
  fluid; we prioritise measurability, cost control, and testability for v1.
