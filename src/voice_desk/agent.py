"""Agent entrypoint: build the voice pipeline, wire the session, run a call.

This module owns everything that touches a live LiveKit session — the provider-
agnostic STT/LLM/TTS assembly, per-call metrics and transcript hooks, the
greeting, and graceful end-of-call handling. The conversation logic itself lives
in :mod:`voice_desk.tools` and :mod:`voice_desk.prompts`; the evals drive those
directly without any of the audio machinery here.
"""

from __future__ import annotations

import asyncio
import logging
import time

from livekit.agents import (
    AgentSession,
    AutoSubscribe,
    JobContext,
    JobProcess,
    TurnHandlingOptions,
    inference,
)
from livekit.plugins import silero

from .config import Settings
from .metrics import CallMetrics
from .prompts import GREETING
from .tools import DeskContext, FrontDeskAgent, build_desk_context
from .transcript import CallTranscript

logger = logging.getLogger("voice_desk")

# Phrases that signal the caller is done. The model produces the actual sign-off;
# this only decides when to hang up afterwards.
_FAREWELLS = (
    "goodbye",
    "bye",
    "that's all",
    "thats all",
    "that is all",
    "that's everything",
    "nothing else",
    "no that's it",
    "we're done",
    "were done",
    "that will be all",
    "have a good day",
)


def _is_farewell(text: str) -> bool:
    lowered = text.strip().lower()
    return any(phrase in lowered for phrase in _FAREWELLS)


# -- pipeline assembly ------------------------------------------------------
def _build_stt(settings: Settings):
    if settings.stt_provider == "deepgram":
        from livekit.plugins import deepgram

        return deepgram.STT(
            model=settings.stt_model or "nova-3",
            language=settings.stt_language or "en-US",
        )
    from livekit.plugins import openai

    return openai.STT(
        model=settings.stt_model or "gpt-4o-mini-transcribe",
        language=settings.stt_language or "en",
    )


def _build_tts(settings: Settings):
    if settings.tts_provider == "cartesia":
        from livekit.plugins import cartesia

        return cartesia.TTS(
            model=settings.tts_model or "sonic-3",
            voice=settings.resolved_tts_voice,
            language="en",
        )
    from livekit.plugins import openai

    return openai.TTS(
        model=settings.tts_model or "gpt-4o-mini-tts",
        voice=settings.resolved_tts_voice,
    )


def _build_llm(settings: Settings):
    from voice_desk.llm import build_agent_llm

    return build_agent_llm(settings.llm_model)


def _turn_handling(settings: Settings) -> TurnHandlingOptions:
    """Choose the turn-detection strategy.

    Default is VAD-based endpointing — fully offline and dependency-free, so the
    console demo needs no LiveKit account. ``TURN_DETECTION=model`` opts into the
    1.6 audio-native Turn Detector (``inference.TurnDetector``), which is more
    accurate but requires LiveKit inference credentials (it downloads/loads a
    model). We fall back to VAD if it can't be constructed. The deprecated
    text-based MultilingualModel detector is intentionally not used.
    """
    if settings.turn_detection == "model":
        try:
            return TurnHandlingOptions(turn_detection=inference.TurnDetector())
        except Exception as exc:  # pragma: no cover - depends on external creds
            logger.warning("turn detector unavailable (%s); falling back to VAD", exc)
    return TurnHandlingOptions(turn_detection="vad")


def build_session(
    settings: Settings, userdata: DeskContext, *, vad=None
) -> AgentSession[DeskContext]:
    """Assemble an AgentSession from the selected stack."""
    from voice_desk.llm import MAX_TOOL_STEPS

    return AgentSession[DeskContext](
        stt=_build_stt(settings),
        llm=_build_llm(settings),
        tts=_build_tts(settings),
        vad=vad or silero.VAD.load(),
        turn_handling=_turn_handling(settings),
        userdata=userdata,
        max_tool_steps=MAX_TOOL_STEPS,
    )


def prewarm(proc: JobProcess) -> None:
    """Load the Silero VAD once per worker process (saves ~200 ms on first turn)."""
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext) -> None:
    """Handle a single inbound call."""
    settings = Settings.from_env()
    userdata = build_desk_context(settings)

    room_name = ctx.room.name if ctx.room else "call"
    call_id = f"{room_name}-{int(time.time())}".replace(" ", "_")

    call_metrics = CallMetrics(call_id=call_id, metrics_dir=settings.metrics_dir)
    transcript = CallTranscript(call_id=call_id, transcript_dir=settings.transcript_dir)

    vad = ctx.proc.userdata.get("vad") if ctx.proc else None
    session = build_session(settings, userdata, vad=vad)

    call_metrics.attach(session)
    transcript.attach(session)

    ending = {"pending": False, "closing": False}

    @session.on("user_input_transcribed")
    def _on_user(ev) -> None:
        if getattr(ev, "is_final", False) and _is_farewell(getattr(ev, "transcript", "")):
            ending["pending"] = True

    @session.on("conversation_item_added")
    def _on_item(ev) -> None:
        item = getattr(ev, "item", None)
        if getattr(item, "role", None) == "assistant" and ending["pending"]:
            ending["pending"] = False
            if not ending["closing"]:
                ending["closing"] = True
                asyncio.create_task(_graceful_end())

    async def _graceful_end() -> None:
        # Let the sign-off finish playing before hanging up.
        try:
            await session.drain()
        except Exception:  # pragma: no cover - best-effort during teardown
            logger.debug("drain during graceful end failed", exc_info=True)
        ctx.shutdown(reason="caller ended the call")

    async def _finalize(_reason: str = "") -> None:
        call_metrics.finalize()
        transcript.finalize()

    ctx.add_shutdown_callback(_finalize)

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    await session.start(agent=FrontDeskAgent(), room=ctx.room)
    await session.say(GREETING, allow_interruptions=True)
