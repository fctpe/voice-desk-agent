"""Latency metrics: percentile math and per-turn correlation by speech_id."""

from __future__ import annotations

from livekit.agents import metrics as lk_metrics

from voice_desk.metrics import CallMetrics, TurnLatency, percentile


def test_percentile_empty():
    assert percentile([], 50) is None


def test_percentile_single():
    assert percentile([42.0], 95) == 42.0


def test_percentile_interpolates():
    values = [100.0, 200.0, 300.0, 400.0]
    assert percentile(values, 50) == 250.0
    # p95 of 4 points sits near the top of the range.
    assert percentile(values, 95) == 385.0


def test_turn_response_requires_core_stages():
    incomplete = TurnLatency(speech_id="s1", eou_delay_ms=100.0, llm_ttft_ms=None)
    assert incomplete.response_ms is None
    complete = TurnLatency(speech_id="s1", eou_delay_ms=100.0, llm_ttft_ms=200.0, tts_ttfb_ms=150.0)
    assert complete.response_ms == 450.0


def _eou(speech_id: str, eou: float, transcription: float) -> lk_metrics.EOUMetrics:
    return lk_metrics.EOUMetrics(
        timestamp=0.0,
        end_of_utterance_delay=eou,
        transcription_delay=transcription,
        on_user_turn_completed_delay=0.0,
        speech_id=speech_id,
    )


def _llm(speech_id: str, ttft: float) -> lk_metrics.LLMMetrics:
    return lk_metrics.LLMMetrics(
        label="llm",
        request_id="r",
        timestamp=0.0,
        duration=1.0,
        ttft=ttft,
        cancelled=False,
        completion_tokens=10,
        prompt_tokens=20,
        prompt_cached_tokens=0,
        total_tokens=30,
        tokens_per_second=10.0,
        speech_id=speech_id,
    )


def _tts(speech_id: str, ttfb: float) -> lk_metrics.TTSMetrics:
    return lk_metrics.TTSMetrics(
        label="tts",
        request_id="r",
        timestamp=0.0,
        ttfb=ttfb,
        duration=1.0,
        audio_duration=1.0,
        cancelled=False,
        characters_count=42,
        streamed=True,
        speech_id=speech_id,
    )


def test_record_correlates_by_speech_id(tmp_path):
    cm = CallMetrics(call_id="test", metrics_dir=tmp_path)
    # Two turns, metrics arriving interleaved as they would from the SDK.
    cm.record(_eou("turn-a", eou=0.1, transcription=0.05))
    cm.record(_llm("turn-a", ttft=0.2))
    cm.record(_tts("turn-a", ttfb=0.15))
    cm.record(_eou("turn-b", eou=0.12, transcription=0.06))
    cm.record(_llm("turn-b", ttft=0.25))
    cm.record(_tts("turn-b", ttfb=0.18))

    summary = cm.summary()
    assert summary["turns"] == 2
    # turn-a response = 100 + 200 + 150 = 450 ms; turn-b = 120 + 250 + 180 = 550 ms
    resp = summary["percentiles"]["response_ms"]
    assert resp["p50"] == 500.0

    report = cm.finalize(echo=False)
    assert (tmp_path / "metrics-test.json").exists()
    assert report["summary"]["turns"] == 2
