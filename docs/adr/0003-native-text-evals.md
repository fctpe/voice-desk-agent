# ADR-0003: Native text-mode evals over audio-simulation SaaS

## Status
Accepted.

## Context
Conversation quality needs regression testing. Options considered:
1. A third-party **audio-simulation SaaS** that places synthetic voice calls and
   scores them.
2. Home-grown audio playback against a running agent.
3. LiveKit Agents' **native testing API** (`AgentSession.run(user_input=...)`,
   `result.expect...`, `.judge(llm, intent=...)`, `mock_tools`), driving the
   agent over text.

## Decision
Use the native text-mode testing API (option 3).

## Consequences
- **No audio, no accounts, no per-call cost** beyond the LLM + judge tokens. The
  evals run in CI gated only on `OPENAI_API_KEY`.
- **Real agent, real tools.** `session.run` exercises the actual prompt, tool
  schemas, and the deterministic providers — not a mock of the agent. Assertions
  target the true event stream (function calls) and an LLM judges the final
  reply's intent.
- **Fast and deterministic-ish.** Text turns are quick; the fakes are
  deterministic, so only the model's wording varies, which the judges tolerate.
- **Trade-off accepted.** Text mode does not exercise STT/TTS or barge-in/turn
  timing. Those are audio concerns better observed live (console) and via the
  latency metrics; the evals own *conversation correctness*.
