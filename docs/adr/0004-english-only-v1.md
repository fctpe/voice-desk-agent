# ADR-0004: English-only global positioning for v1

## Status
Accepted.

## Context
A voice front desk could be multilingual from day one. But every added language
multiplies the surface that must be prompt-engineered, voiced, and — most
importantly — **evaluated**. Read-back rules, emergency phrasing, and
injection-resistance all need per-language test coverage to be trustworthy.

## Decision
Ship v1 as **English-only**, positioned for a global English-speaking audience
(the fictional business, persona, and prompts are all English).

## Consequences
- The prompt, the 10 eval scenarios, and the read-back helpers can be sharp and
  well-tested rather than shallow across many languages.
- STT/TTS defaults (Deepgram nova-3 `en`, Cartesia sonic-3 English voice, OpenAI
  English) are chosen for quality in one language.
- **Roadmap, not a wall.** The pipeline is provider-agnostic and the turn
  detector is configurable; adding a language is a matter of localized prompts +
  a matching STT/TTS locale + a parallel eval set. The 1.6 audio-native turn
  detector is multilingual-capable when enabled. Multilingual is deferred to a
  future version with its own eval suite, not bolted on untested.
