"""OpenTelemetry export, opt-in via ``OTEL_EXPORTER_OTLP_ENDPOINT``.

livekit-agents 1.6.5 already opens the spans this project wants: one
``agent_turn`` per turn (tagged ``lk.speech_id``), one ``llm_request`` per model
call (tagged ``gen_ai.operation.name`` / ``gen_ai.request.model`` /
``gen_ai.provider.name`` / ``gen_ai.usage.input_tokens`` /
``gen_ai.usage.output_tokens``) and one ``function_tool`` per tool call. They all
go to a no-op tracer until a provider is installed, so this module installs one
instead of emitting a second, parallel span tree over the same call.

The one gap is the tool span: LiveKit names it ``lk.function_tool.name``, which a
backend that speaks the GenAI conventions does not look at. :func:`traced_tool`
adds the GenAI names to that same span.

With ``OTEL_EXPORTER_OTLP_ENDPOINT`` unset, no provider is installed and no
exporter is built: LiveKit still opens its spans, but they are the API's
``NonRecordingSpan`` singleton, they carry no attributes and they go nowhere, and
:func:`traced_tool` returns on its first branch. The OTel SDK and the OTLP
exporter are loaded either way — ``livekit.agents.telemetry.traces`` imports them
at module scope, so importing this project's agent pulls them in whatever the
setting says (measured in docs/adr/0007).
"""

from __future__ import annotations

import functools
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from livekit.agents.telemetry import set_tracer_provider
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger("voice_desk")

# GenAI semantic-convention attribute names. Still "Development" status as of
# 2026-07, which is why they are spelled out here rather than imported from a
# pinned conventions package — there is no tagged release to pin (ADR-0007).
_ATTR_OPERATION_NAME = "gen_ai.operation.name"
_ATTR_TOOL_NAME = "gen_ai.tool.name"
# LiveKit's correlation key, repeated onto the tool span so a tool call can be
# filtered to its turn directly instead of walking up to the parent agent_turn.
_ATTR_SPEECH_ID = "lk.speech_id"

_provider: TracerProvider | None = None


def setup_tracing() -> bool:
    """Send LiveKit's spans to the OTLP endpoint; return whether tracing is on.

    Idempotent, because the provider is process-wide: a worker handling a second
    call must not stack another exporter onto the same spans.
    """
    global _provider
    if _provider is not None:
        return True
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip():
        return False

    # Resource.create() reads OTEL_SERVICE_NAME, but an explicit attribute beats
    # it, so only name the service when the operator has not.
    named = {} if os.getenv("OTEL_SERVICE_NAME") else {SERVICE_NAME: "voice-desk-agent"}
    provider = TracerProvider(resource=Resource.create(named))
    # OTLPSpanExporter() reads the endpoint and headers from the standard env.
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    set_tracer_provider(provider)
    _provider = provider
    logger.info("OTLP tracing enabled")
    return True


def flush_tracing(timeout_ms: int = 5_000) -> None:
    """Push buffered spans out at end of call. No-op when tracing is off.

    Without this the last turns of a call sit in the batch processor's queue and
    a console run that exits promptly loses them.
    """
    if _provider is not None:
        _provider.force_flush(timeout_ms)


def traced_tool(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Add the GenAI tool attributes to LiveKit's ``function_tool`` span.

    Stacks *under* ``@function_tool``: LiveKit runs the tool body inside that
    span, so the current span here is the tool's own. Reads nothing off the
    context until it knows a real span is listening, which keeps the tools
    callable from unit tests with a stub ``RunContext``.
    """

    @functools.wraps(fn)
    async def _traced(self: Any, ctx: Any, *args: Any, **kwargs: Any) -> Any:
        span = trace.get_current_span()
        if span.is_recording():
            span.set_attributes(
                {
                    _ATTR_OPERATION_NAME: "execute_tool",
                    _ATTR_TOOL_NAME: ctx.function_call.name,
                    _ATTR_SPEECH_ID: ctx.speech_handle.id,
                }
            )
        return await fn(self, ctx, *args, **kwargs)

    return _traced
