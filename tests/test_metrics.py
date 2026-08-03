"""Latency/token capture: percentile math and the ChatMessage.metrics shape."""

from __future__ import annotations

from livekit.agents.llm import ChatMessage
from livekit.agents.metrics import AgentSessionUsage, LLMModelUsage, TTSModelUsage

from voice_desk.metrics import CallMetrics, percentile


def test_percentile_empty():
    assert percentile([], 50) is None


def test_percentile_single():
    assert percentile([42.0], 95) == 42.0


def test_percentile_interpolates():
    values = [100.0, 200.0, 300.0, 400.0]
    assert percentile(values, 50) == 250.0
    # p95 of 4 points sits near the top of the range.
    assert percentile(values, 95) == 385.0


def _user(item_id: str, *, eou: float, transcription: float) -> ChatMessage:
    return ChatMessage(
        id=item_id,
        role="user",
        content=["hello"],
        metrics={"end_of_turn_delay": eou, "transcription_delay": transcription},
    )


def _assistant(item_id: str, *, ttft: float, ttfb: float, e2e: float) -> ChatMessage:
    return ChatMessage(
        id=item_id,
        role="assistant",
        content=["hi there"],
        metrics={
            "llm_node_ttft": ttft,
            "tts_node_ttfb": ttfb,
            "e2e_latency": e2e,
            "llm_metadata": {"model_name": "gpt-4.1", "model_provider": "openai"},
        },
    )


def test_records_both_message_roles(tmp_path):
    cm = CallMetrics(call_id="test", metrics_dir=tmp_path)
    cm.record_item(_user("u1", eou=0.1, transcription=0.05))
    cm.record_item(_assistant("a1", ttft=0.2, ttfb=0.15, e2e=0.45))
    cm.record_item(_user("u2", eou=0.12, transcription=0.06))
    cm.record_item(_assistant("a2", ttft=0.25, ttfb=0.18, e2e=0.55))

    summary = cm.summary()
    # Only assistant messages are agent turns; user rows carry the input stages.
    assert summary["turns"] == 2
    assert summary["percentiles"]["response_ms"]["p50"] == 500.0
    assert summary["percentiles"]["eou_delay_ms"]["p50"] == 110.0

    report = cm.finalize(echo=False)
    assert (tmp_path / "metrics-test.json").exists()
    assert report["turns"][1] == {
        "item_id": "a1",
        "role": "assistant",
        "eou_delay_ms": None,
        "transcription_delay_ms": None,
        "llm_ttft_ms": 200.0,
        "tts_ttfb_ms": 150.0,
        "response_ms": 450.0,
        "model": "gpt-4.1",
    }


def test_skips_items_without_metrics(tmp_path):
    """Handoffs and text-mode user messages carry no metrics; they must not pad
    the turn count with a row of Nones."""
    cm = CallMetrics(call_id="test", metrics_dir=tmp_path)
    cm.record_item(ChatMessage(id="a1", role="assistant", content=["hi"]))
    cm.record_item(None)
    assert cm.summary()["turns"] == 0


def test_usage_snapshot_replaces_rather_than_accumulates(tmp_path, capsys):
    """AgentSessionUsage is a running session total, not a per-event delta —
    summing successive events would multiply the bill."""
    cm = CallMetrics(call_id="test", metrics_dir=tmp_path)
    for output in (7, 12):
        cm.record_usage(
            AgentSessionUsage(
                model_usage=[
                    LLMModelUsage(
                        provider="openai", model="gpt-4.1", input_tokens=32, output_tokens=output
                    ),
                    TTSModelUsage(provider="openai", model="gpt-4o-mini-tts", characters_count=90),
                ]
            )
        )

    usage = cm.as_dict()["usage"]
    assert [row["type"] for row in usage] == ["llm_usage", "tts_usage"]
    assert usage[0]["input_tokens"] == 32
    assert usage[0]["output_tokens"] == 12

    cm.finalize(write=False)
    assert "LLM tokens in=32 out=12" in capsys.readouterr().out


def test_echo_reports_unknown_tokens_as_na(tmp_path, capsys):
    """A call with no usage event costs an unknown amount, not nothing — "in=0
    out=0" on a line whose every latency reads "n/a" claims otherwise."""
    CallMetrics(call_id="test", metrics_dir=tmp_path).finalize(write=False)
    assert "LLM tokens in=n/a out=n/a" in capsys.readouterr().out
