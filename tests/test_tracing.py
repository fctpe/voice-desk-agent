"""OTel wiring, and the LiveKit 1.6.5 span/metrics contract it depends on.

The whole design rests on livekit-agents emitting the GenAI-attributed spans
itself once a provider is installed (docs/adr/0007). That is a third-party
behaviour, so it is asserted here rather than assumed: a scripted fake LLM drives
the real agent and the real tools through one tool-calling turn, offline.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from livekit.agents import AgentSession, llm
from livekit.agents.llm import ChatChunk, ChoiceDelta, FunctionToolCall, LLMStream
from livekit.agents.telemetry import set_tracer_provider
from livekit.agents.telemetry import tracer as lk_tracer
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from voice_desk import tracing
from voice_desk.config import Settings
from voice_desk.metrics import CallMetrics
from voice_desk.tools import DeskContext, FrontDeskAgent, build_desk_context


class _ScriptedStream(LLMStream):
    def __init__(self, source, *, chat_ctx, tools, conn_options, chunks):
        super().__init__(source, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._chunks = chunks

    async def _run(self) -> None:
        for chunk in self._chunks:
            self._event_ch.send_nowait(chunk)


class ScriptedLLM(llm.LLM):
    """Calls check_availability on the first request, then answers. No network."""

    def __init__(self) -> None:
        super().__init__()
        self._requests = 0

    @property
    def model(self) -> str:
        return "scripted-model"

    @property
    def provider(self) -> str:
        return "scripted-provider"

    def chat(self, *, chat_ctx, tools=None, conn_options=DEFAULT_API_CONNECT_OPTIONS, **kwargs):
        self._requests += 1
        if self._requests == 1:
            delta = ChoiceDelta(
                role="assistant",
                tool_calls=[
                    FunctionToolCall(
                        call_id="call_1",
                        name="check_availability",
                        arguments='{"day_preference": "tomorrow", "time_window": "morning"}',
                    )
                ],
            )
            usage = llm.CompletionUsage(
                completion_tokens=7, prompt_tokens=11, prompt_cached_tokens=0, total_tokens=18
            )
        else:
            delta = ChoiceDelta(role="assistant", content="I have a nine o'clock free.")
            usage = llm.CompletionUsage(
                completion_tokens=5, prompt_tokens=21, prompt_cached_tokens=0, total_tokens=26
            )
        chunks = [ChatChunk(id="c", delta=delta), ChatChunk(id="c", usage=usage)]
        return _ScriptedStream(
            self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options, chunks=chunks
        )


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Install a recording tracer provider, and put LiveKit's back afterwards.

    ``set_tracer_provider`` mutates a process-wide tracer, so leaving it in place
    would make every later test's tool calls hit a recording span.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_provider", provider)
    set_tracer_provider(provider)
    yield exporter
    set_tracer_provider(trace.NoOpTracerProvider())


def _attrs(exporter: InMemorySpanExporter, name: str) -> dict:
    for span in exporter.get_finished_spans():
        if span.name == name:
            return dict(span.attributes or {})
    raise AssertionError(f"no {name!r} span; got {[s.name for s in exporter.get_finished_spans()]}")


async def _run_one_turn(tmp_path: Path, fixed_now) -> tuple[CallMetrics, AgentSession]:
    settings = Settings(outbox_dir=str(tmp_path / "outbox"))
    userdata = build_desk_context(settings, now=fixed_now)
    session: AgentSession[DeskContext] = AgentSession(
        llm=ScriptedLLM(), userdata=userdata, max_tool_steps=4
    )
    metrics = CallMetrics(call_id="trace-test", metrics_dir=tmp_path)
    metrics.attach(session)
    await session.start(agent=FrontDeskAgent())
    await session.run(user_input="anything free tomorrow morning?")
    await session.aclose()
    return metrics, session


def test_disabled_without_endpoint(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(tracing, "_provider", None)
    assert tracing.setup_tracing() is False
    # A missing endpoint must not leave a provider behind to flush.
    assert tracing._provider is None
    tracing.flush_tracing()


def test_what_disabled_actually_means():
    """The docs claim a precise thing about the off state, so measure it rather
    than assert it: LiveKit's spans are still opened, they are just the API's
    non-recording singleton — and the OTLP SDK is loaded either way, because
    livekit.agents.telemetry imports it at module scope (docs/adr/0007)."""
    with lk_tracer.start_as_current_span("agent_turn") as span:
        assert span.is_recording() is False
        assert span.get_span_context().is_valid is False
    assert not isinstance(trace.get_tracer_provider(), TracerProvider)
    assert "opentelemetry.exporter.otlp.proto.http.trace_exporter" in sys.modules


async def test_traced_tool_is_a_no_op_without_a_recording_span(tmp_path, fixed_now):
    """The tools stay callable with a bare context — nothing is read off it
    until a real span is listening."""

    class StubRunContext:
        def __init__(self, userdata):
            self.userdata = userdata

    settings = Settings(outbox_dir=str(tmp_path / "outbox"))
    ctx = StubRunContext(build_desk_context(settings, now=fixed_now))
    out = await FrontDeskAgent().check_availability(
        ctx, day_preference="tomorrow", time_window="morning"
    )
    assert out


def test_traced_tool_preserves_the_model_facing_schema():
    """``@traced_tool`` sits under ``@function_tool``, which builds each tool's
    JSON schema from its signature and docstring. A wrapper that swallowed either
    would break every tool call, and only a live eval would notice."""
    from livekit.agents.llm import utils as llm_utils

    schemas = {}
    for tool in FrontDeskAgent().tools:
        fn = llm_utils.build_legacy_openai_schema(tool)["function"]
        schemas[fn["name"]] = fn
    assert sorted(schemas) == [
        "book_appointment",
        "cancel_appointment",
        "check_availability",
        "identify_caller",
        "reschedule_appointment",
        "send_confirmation",
        "take_message",
    ]
    booking = schemas["book_appointment"]
    assert sorted(booking["parameters"]["properties"]) == ["issue", "name", "phone", "slot_id"]
    assert booking["description"]


async def test_livekit_emits_genai_spans(spans, tmp_path, fixed_now):
    await _run_one_turn(tmp_path, fixed_now)
    tracing.flush_tracing()

    # The turn span is LiveKit's, and carries the speech_id everything else
    # correlates by.
    speech_id = _attrs(spans, "agent_turn")["lk.speech_id"]
    assert speech_id

    # The model call: LiveKit already attributes it per the GenAI conventions,
    # token counts included — which is why voice_desk does not re-emit it.
    request = _attrs(spans, "llm_request")
    assert request["gen_ai.operation.name"] == "chat"
    assert request["gen_ai.request.model"] == "scripted-model"
    assert request["gen_ai.usage.input_tokens"] == 11
    assert request["gen_ai.usage.output_tokens"] == 7

    # The tool call: the GenAI names are the part voice_desk adds.
    tool = _attrs(spans, "function_tool")
    assert tool["gen_ai.operation.name"] == "execute_tool"
    assert tool["gen_ai.tool.name"] == "check_availability"
    assert tool["lk.speech_id"] == speech_id


async def test_call_metrics_capture_tokens_without_the_deprecated_event(
    tmp_path, fixed_now, caplog
):
    metrics, _ = await _run_one_turn(tmp_path, fixed_now)
    report = metrics.finalize(write=False, echo=False)

    # Cumulative across both model calls: 11+21 in, 7+5 out.
    assert report["usage"] == [
        {
            "type": "llm_usage",
            "provider": "scripted-provider",
            "model": "scripted-model",
            "input_tokens": 32,
            "input_cached_tokens": 0,
            "input_audio_tokens": 0,
            "input_cached_audio_tokens": 0,
            "input_text_tokens": 0,
            "input_cached_text_tokens": 0,
            "input_image_tokens": 0,
            "input_cached_image_tokens": 0,
            "output_tokens": 12,
            "output_audio_tokens": 0,
            "output_text_tokens": 0,
            "session_duration": 0.0,
        }
    ]
    assert report["summary"]["turns"] == 1
    assert report["turns"][0]["model"] == "scripted-model"
    # The eval harness's only latency sample; text mode wires no STT/TTS, so
    # every other stage is legitimately absent.
    assert report["turns"][0]["llm_ttft_ms"] is not None
    assert report["turns"][0]["response_ms"] is None
    assert "metrics_collected is deprecated" not in caplog.text
