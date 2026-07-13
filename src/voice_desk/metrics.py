"""Per-turn latency capture from LiveKit's ``metrics_collected`` events.

LiveKit emits a metric object per pipeline stage (EOU decision, LLM, TTS, STT).
We correlate them by ``speech_id`` into one row per agent turn and derive the
number that actually governs how "instant" the agent feels:

    EOU -> first audio  =  end_of_utterance_delay + LLM TTFT + TTS TTFB

i.e. from the moment the caller stops talking to the first byte of the reply.
The human-conversation comfort zone is roughly 500-800 ms; anything much beyond
that reads as a laggy call. At the end of a call we print a p50/p95 summary and
write a JSON report so the budget is measurable, not vibes.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from livekit.agents import metrics as lk_metrics


def _ms(seconds: float | None) -> float | None:
    if seconds is None or seconds < 0:
        return None
    return round(seconds * 1000.0, 1)


def _percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile (no numpy dependency)."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    rank = pct / 100.0 * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return round(ordered[lo] * (1 - frac) + ordered[hi] * frac, 1)


@dataclass
class TurnLatency:
    """One agent turn's stage latencies (milliseconds)."""

    speech_id: str
    eou_delay_ms: float | None = None
    transcription_delay_ms: float | None = None
    llm_ttft_ms: float | None = None
    tts_ttfb_ms: float | None = None

    @property
    def response_ms(self) -> float | None:
        """EOU -> first audio budget for this turn, when the core stages are present."""
        if self.llm_ttft_ms is None or self.tts_ttfb_ms is None:
            return None
        return round((self.eou_delay_ms or 0.0) + self.llm_ttft_ms + self.tts_ttfb_ms, 1)


class CallMetrics:
    """Accumulates metric events for a single call and reports at the end."""

    def __init__(self, *, call_id: str, metrics_dir: str | Path = "metrics") -> None:
        self.call_id = call_id
        self._dir = Path(metrics_dir)
        self._turns: dict[str, TurnLatency] = {}
        self._fallback_seq = 0

    # -- ingestion ----------------------------------------------------------
    def attach(self, session: Any) -> None:
        """Subscribe to a live AgentSession's metrics stream."""

        @session.on("metrics_collected")
        def _on_metrics(ev: Any) -> None:  # pragma: no cover - exercised in live runs
            self.record(ev.metrics)

    def _turn(self, speech_id: str | None) -> TurnLatency:
        if not speech_id:
            self._fallback_seq += 1
            speech_id = f"turn-{self._fallback_seq}"
        if speech_id not in self._turns:
            self._turns[speech_id] = TurnLatency(speech_id=speech_id)
        return self._turns[speech_id]

    def record(self, metric: Any) -> None:
        """Fold a single metric object into the per-turn table."""
        if isinstance(metric, lk_metrics.EOUMetrics):
            turn = self._turn(getattr(metric, "speech_id", None))
            turn.eou_delay_ms = _ms(getattr(metric, "end_of_utterance_delay", None))
            turn.transcription_delay_ms = _ms(getattr(metric, "transcription_delay", None))
        elif isinstance(metric, lk_metrics.LLMMetrics):
            turn = self._turn(getattr(metric, "speech_id", None))
            # A tool-calling turn fires several LLMMetrics; the caller waits on
            # the FIRST token, so keep the first TTFT, not the last step's.
            if turn.llm_ttft_ms is None:
                turn.llm_ttft_ms = _ms(getattr(metric, "ttft", None))
        elif isinstance(metric, lk_metrics.TTSMetrics):
            turn = self._turn(getattr(metric, "speech_id", None))
            # Likewise keep the first spoken audio's TTFB.
            if turn.tts_ttfb_ms is None:
                turn.tts_ttfb_ms = _ms(getattr(metric, "ttfb", None))
        # STT/VAD metrics are captured implicitly via EOU; ignored here to keep
        # the report focused on the response-latency budget.

    # -- reporting ----------------------------------------------------------
    def _column(self, attr: str) -> list[float]:
        out = []
        for turn in self._turns.values():
            value = getattr(turn, attr)
            if value is not None:
                out.append(value)
        return out

    def summary(self) -> dict[str, Any]:
        responses = [t.response_ms for t in self._turns.values() if t.response_ms is not None]
        stages = {
            "eou_delay_ms": self._column("eou_delay_ms"),
            "transcription_delay_ms": self._column("transcription_delay_ms"),
            "llm_ttft_ms": self._column("llm_ttft_ms"),
            "tts_ttfb_ms": self._column("tts_ttfb_ms"),
            "response_ms": responses,
        }
        return {
            "call_id": self.call_id,
            "turns": len(self._turns),
            "percentiles": {
                name: {"p50": _percentile(vals, 50), "p95": _percentile(vals, 95)}
                for name, vals in stages.items()
            },
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "generated_at": time.time(),
            "turns": [asdict(t) | {"response_ms": t.response_ms} for t in self._turns.values()],
            "summary": self.summary(),
        }

    def finalize(self, *, write: bool = True, echo: bool = True) -> dict[str, Any]:
        """Write the JSON report and print a compact p50/p95 line."""
        report = self.as_dict()
        if write:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / f"metrics-{self.call_id}.json"
            path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        if echo:
            self._echo(report["summary"])
        return report

    @staticmethod
    def _echo(summary: dict[str, Any]) -> None:
        pct = summary["percentiles"]
        resp = pct["response_ms"]

        def fmt(v: float | None) -> str:
            return f"{v:.0f}ms" if v is not None else "n/a"

        print(
            f"[METRICS] call={summary['call_id']} turns={summary['turns']} "
            f"EOU->audio p50={fmt(resp['p50'])} p95={fmt(resp['p95'])} | "
            f"LLM TTFT p50={fmt(pct['llm_ttft_ms']['p50'])} "
            f"TTS TTFB p50={fmt(pct['tts_ttfb_ms']['p50'])}"
        )
