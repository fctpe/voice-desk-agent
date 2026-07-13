"""Environment-driven configuration.

Kept dependency-light on purpose: plain :mod:`dataclasses` + ``os.environ`` so the
settings object is trivially constructable in unit tests without touching the
network or a real ``.env`` file. Two presets are exposed through
``VOICE_DESK_STACK``:

* ``openai`` (default) — STT, LLM and TTS all served by OpenAI, so a single
  ``OPENAI_API_KEY`` runs the console demo. Zero-friction.
* ``full`` — Deepgram STT (nova-3) + OpenAI LLM + Cartesia TTS (sonic-3). Lower
  latency and per-stage cost visibility; needs three keys.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

Stack = Literal["openai", "full"]
FailMode = Literal["none", "calendar", "confirmation", "contacts"]
TurnDetection = Literal["vad", "model"]


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _int(name: str, default: int) -> int:
    raw = _clean(os.getenv(name))
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the runtime configuration."""

    stack: Stack = "openai"

    # Voice pipeline models (overridable independently of the stack preset).
    # gpt-4.1, not -mini: evals showed mini narrates fake availability and
    # "booked" confirmations without calling the tools. gpt-4.1 with
    # sequential tool calls (see voice_desk.llm) matches LiveKit's own
    # frontdesk example and books reliably. See docs/adr/0005.
    llm_model: str = "gpt-4.1"
    # Judge for the eval harness, decoupled from the agent model so scores stay
    # comparable when the agent model changes.
    judge_model: str = "gpt-4o-mini"
    stt_model: str = ""
    stt_language: str = "en"
    tts_model: str = ""
    tts_voice: str = ""

    turn_detection: TurnDetection = "vad"

    # Credentials (never logged).
    openai_api_key: str = ""
    deepgram_api_key: str = ""
    cartesia_api_key: str = ""
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""

    # Runtime output directories.
    outbox_dir: str = "outbox"
    transcript_dir: str = "transcripts"
    metrics_dir: str = "metrics"

    # Deterministic demo data + resilience-demo knobs.
    demo_seed: int = 42
    demo_latency_ms: int = 0
    demo_fail: FailMode = "none"

    # Public Cartesia plugin default ("Katie — Friendly Fixer"). Shipped as the
    # documented default so the `full` stack works out of the box; override with
    # TTS_VOICE. This is the plugin's own published default id.
    _cartesia_default_voice: str = field(default="f786b574-daa5-4673-aa0c-cbe3e8534c02", repr=False)

    @classmethod
    def from_env(cls) -> Settings:
        stack_raw = _clean(os.getenv("VOICE_DESK_STACK")).lower() or "openai"
        stack: Stack = "full" if stack_raw == "full" else "openai"

        turn_raw = _clean(os.getenv("TURN_DETECTION")).lower()
        turn: TurnDetection = "model" if turn_raw == "model" else "vad"

        fail_raw = _clean(os.getenv("DEMO_FAIL")).lower() or "none"
        fail: FailMode = (
            fail_raw if fail_raw in ("calendar", "confirmation", "contacts") else "none"
        )

        return cls(
            stack=stack,
            llm_model=_clean(os.getenv("LLM_MODEL")) or "gpt-4.1",
            judge_model=_clean(os.getenv("JUDGE_MODEL")) or "gpt-4o-mini",
            stt_model=_clean(os.getenv("STT_MODEL")),
            stt_language=_clean(os.getenv("STT_LANGUAGE")) or "en",
            tts_model=_clean(os.getenv("TTS_MODEL")),
            tts_voice=_clean(os.getenv("TTS_VOICE")),
            turn_detection=turn,
            openai_api_key=_clean(os.getenv("OPENAI_API_KEY")),
            deepgram_api_key=_clean(os.getenv("DEEPGRAM_API_KEY")),
            cartesia_api_key=_clean(os.getenv("CARTESIA_API_KEY")),
            livekit_url=_clean(os.getenv("LIVEKIT_URL")),
            livekit_api_key=_clean(os.getenv("LIVEKIT_API_KEY")),
            livekit_api_secret=_clean(os.getenv("LIVEKIT_API_SECRET")),
            outbox_dir=_clean(os.getenv("OUTBOX_DIR")) or "outbox",
            transcript_dir=_clean(os.getenv("TRANSCRIPT_DIR")) or "transcripts",
            metrics_dir=_clean(os.getenv("METRICS_DIR")) or "metrics",
            demo_seed=_int("DEMO_SEED", 42),
            demo_latency_ms=_int("DEMO_LATENCY_MS", 0),
            demo_fail=fail,
        )

    # -- Pipeline shape -----------------------------------------------------
    @property
    def stt_provider(self) -> Literal["openai", "deepgram"]:
        return "deepgram" if self.stack == "full" else "openai"

    @property
    def tts_provider(self) -> Literal["openai", "cartesia"]:
        return "cartesia" if self.stack == "full" else "openai"

    @property
    def resolved_tts_voice(self) -> str:
        if self.tts_voice:
            return self.tts_voice
        return self._cartesia_default_voice if self.tts_provider == "cartesia" else "ash"

    def missing_keys(self) -> list[str]:
        """API keys required for the selected stack that are not set.

        Used for a fast, friendly pre-flight check before the agent boots — so a
        keyless console run prints guidance instead of a mid-connection traceback.
        """
        missing: list[str] = []
        if not self.openai_api_key:
            # Needed for the LLM in both stacks (and STT+TTS in the openai stack).
            missing.append("OPENAI_API_KEY")
        if self.stack == "full":
            if not self.deepgram_api_key:
                missing.append("DEEPGRAM_API_KEY")
            if not self.cartesia_api_key:
                missing.append("CARTESIA_API_KEY")
        return missing
