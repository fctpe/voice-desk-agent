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

from livekit import rtc
from livekit.agents import (
    AgentSession,
    AutoSubscribe,
    JobContext,
    JobProcess,
    TurnHandlingOptions,
    inference,
)

# Every provider plugin is imported here, at module scope, and none of them
# lazily inside the build helpers below: importing a plugin registers it, and
# livekit-agents 1.6 refuses to register one off the main thread. `entrypoint`
# runs on a job thread (console mode) so a lazy import there crashes the call
# with "Plugins must be registered on the main thread". This module is imported
# by `__main__` on the main thread, which is exactly where registration belongs.
from livekit.plugins import cartesia, deepgram, openai, silero

from .config import Settings
from .metrics import CallMetrics
from .prompts import GREETING
from .tools import DeskContext, FrontDeskAgent, build_desk_context
from .tracing import flush_tracing, setup_tracing
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


# LiveKit SIP publishes the calling number on the SIP participant under this
# attribute (docs/telephony.md). It is the caller's line, not their claim about
# it, which is why it — and not identify_caller — is the real identity.
_CALLER_ID_ATTRIBUTE = "sip.phoneNumber"

# How long to wait for the SIP participant. It has normally joined before the
# agent is dispatched, but the room's participant list syncs asynchronously, so
# there is a window right after ctx.connect() in which it is not there yet.
_SIP_PARTICIPANT_TIMEOUT = 5.0


async def _caller_id(ctx: JobContext, *, expect_trunk: bool) -> str | None:
    """The calling number from SIP caller-ID, or None if there is none to be had.

    Three different things produce None and each is logged as itself: no trunk is
    configured (the console demo), a trunk is configured but no SIP participant
    joined, and a SIP participant that withholds caller-ID. Only the first is
    routine — the other two mean the call is on a trunk whose identity never
    arrived, and it stays unidentified rather than falling back to the caller's
    own word for who they are.
    """
    if not expect_trunk:
        logger.info("SIP_TRUNK is off; no caller-ID exists, identity will be caller-stated")
        return None
    try:
        participant = await asyncio.wait_for(
            ctx.wait_for_participant(kind=[rtc.ParticipantKind.PARTICIPANT_KIND_SIP]),
            timeout=_SIP_PARTICIPANT_TIMEOUT,
        )
    except TimeoutError:
        logger.error(
            "SIP_TRUNK is on but no SIP participant joined within %.0fs; "
            "the call stays unidentified and can change no appointment",
            _SIP_PARTICIPANT_TIMEOUT,
        )
        return None
    number = participant.attributes.get(_CALLER_ID_ATTRIBUTE, "")
    if not number:
        logger.error(
            "SIP participant %s carries no %s; "
            "the call stays unidentified and can change no appointment",
            participant.identity,
            _CALLER_ID_ATTRIBUTE,
        )
        return None
    return number


# -- pipeline assembly ------------------------------------------------------
def _build_stt(settings: Settings):
    if settings.stt_provider == "deepgram":
        return deepgram.STT(
            model=settings.stt_model or "nova-3",
            language=settings.stt_language or "en-US",
        )
    return openai.STT(
        model=settings.stt_model or "gpt-4o-mini-transcribe",
        language=settings.stt_language or "en",
    )


def _build_tts(settings: Settings):
    if settings.tts_provider == "cartesia":
        return cartesia.TTS(
            model=settings.tts_model or "sonic-3",
            voice=settings.resolved_tts_voice,
            language="en",
        )
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
    setup_tracing()

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
        flush_tracing()

    ctx.add_shutdown_callback(_finalize)

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    # Bind the call's identity before the first turn, and decide whether the
    # caller may supply it themselves: off-trunk that is the only identity there
    # is, on a trunk it is the thing caller-ID exists to replace (ADR-0006).
    userdata.authenticated_phone = await _caller_id(ctx, expect_trunk=settings.sip_trunk)
    userdata.allow_caller_stated_identity = not settings.sip_trunk

    await session.start(agent=FrontDeskAgent(), room=ctx.room)
    await session.say(GREETING, allow_interruptions=True)
