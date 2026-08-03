"""Per-call latency and token capture from LiveKit's session events.

Two streams, both non-deprecated in livekit-agents 1.6.5:

* ``conversation_item_added`` carries ``ChatMessage.metrics`` — the stage
  latencies for that one message. The number that governs how "instant" the
  agent feels is ``e2e_latency`` on an assistant message: from the moment the
  caller stopped talking to the first byte of the reply, tool round-trips
  included. The human-conversation comfort zone is roughly 500-800 ms; much
  beyond that reads as a laggy call.
* ``session_usage_updated`` carries the call's token and audio totals per
  model, which is what makes the cost of a call measurable rather than guessed.

Both replace ``metrics_collected``, which 1.6.5 deprecates. The tradeoff is
recorded in docs/adr/0007: the replacements are per *message* and per *session*,
so neither exposes ``speech_id``, and per-turn token attribution moved to the
OTel ``llm_request`` span (see :mod:`voice_desk.tracing`).

At the end of a call we print a p50/p95 line and write a JSON report so the
budget is measurable, not vibes.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_LATENCY_STAGES = (
    "eou_delay_ms",
    "transcription_delay_ms",
    "llm_ttft_ms",
    "tts_ttfb_ms",
    "response_ms",
)


def _ms(seconds: float | None) -> float | None:
    if seconds is None or seconds < 0:
        return None
    return round(seconds * 1000.0, 1)


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile (no numpy dependency).

    Public because the eval harness reuses it to summarise LLM TTFT across
    scenarios — the same maths, so it must not be reimplemented there.
    """
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
class TurnMetrics:
    """One chat message's stage latencies (milliseconds), as LiveKit reports them.

    A row is either a user message (endpointing and transcription) or an
    assistant message (model, speech and the end-to-end budget) — LiveKit fills a
    disjoint half of ``ChatMessage.metrics`` for each, and pairing them back up
    ourselves would be a guess. ``response_ms`` is LiveKit's own pairing.
    """

    item_id: str
    role: str
    eou_delay_ms: float | None = None
    transcription_delay_ms: float | None = None
    llm_ttft_ms: float | None = None
    tts_ttfb_ms: float | None = None
    response_ms: float | None = None
    model: str | None = None


class CallMetrics:
    """Accumulates a single call's metrics and reports at the end."""

    def __init__(self, *, call_id: str, metrics_dir: str | Path = "metrics") -> None:
        self.call_id = call_id
        self._dir = Path(metrics_dir)
        self._turns: list[TurnMetrics] = []
        self._usage: list[dict[str, Any]] = []

    # -- ingestion ----------------------------------------------------------
    def attach(self, session: Any) -> None:
        """Subscribe to a live AgentSession's metrics streams."""

        @session.on("conversation_item_added")
        def _on_item(ev: Any) -> None:
            self.record_item(getattr(ev, "item", None))

        @session.on("session_usage_updated")
        def _on_usage(ev: Any) -> None:
            self.record_usage(ev.usage)

    def record_item(self, item: Any) -> None:
        """Fold one conversation item's metrics into the per-message table.

        Items with no metrics — agent handoffs, and the *user* messages of a
        text-mode turn, which never went through STT — are skipped rather than
        recorded as a row of ``None`` that would pad the turn count. A text-mode
        assistant message still carries ``llm_node_ttft``, so it is recorded.
        """
        role = getattr(item, "role", None)
        report = getattr(item, "metrics", None) or {}
        if role not in ("user", "assistant") or not report:
            return
        turn = TurnMetrics(item_id=item.id, role=role)
        if role == "user":
            turn.eou_delay_ms = _ms(report.get("end_of_turn_delay"))
            turn.transcription_delay_ms = _ms(report.get("transcription_delay"))
        else:
            turn.llm_ttft_ms = _ms(report.get("llm_node_ttft"))
            turn.tts_ttfb_ms = _ms(report.get("tts_node_ttfb"))
            turn.response_ms = _ms(report.get("e2e_latency"))
            turn.model = (report.get("llm_metadata") or {}).get("model_name")
        self._turns.append(turn)

    def record_usage(self, usage: Any) -> None:
        """Take the latest usage snapshot.

        ``AgentSessionUsage`` is a running total for the whole session, not a
        delta, so the newest snapshot replaces the previous one. Adding them up
        would multiply the bill by the number of events.
        """
        self._usage = [model.model_dump() for model in usage.model_usage]

    # -- reporting ----------------------------------------------------------
    def _column(self, attr: str) -> list[float]:
        out = []
        for turn in self._turns:
            value = getattr(turn, attr)
            if value is not None:
                out.append(value)
        return out

    def summary(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "turns": sum(1 for turn in self._turns if turn.role == "assistant"),
            "percentiles": {
                stage: {
                    "p50": percentile(self._column(stage), 50),
                    "p95": percentile(self._column(stage), 95),
                }
                for stage in _LATENCY_STAGES
            },
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "generated_at": time.time(),
            "turns": [asdict(turn) for turn in self._turns],
            "usage": self._usage,
            "summary": self.summary(),
        }

    def finalize(self, *, write: bool = True, echo: bool = True) -> dict[str, Any]:
        """Write the JSON report and print a compact p50/p95 + token line."""
        report = self.as_dict()
        if write:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / f"metrics-{self.call_id}.json"
            path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        if echo:
            self._echo(report)
        return report

    @staticmethod
    def _echo(report: dict[str, Any]) -> None:
        summary = report["summary"]
        pct = summary["percentiles"]
        resp = pct["response_ms"]
        llm = [row for row in report["usage"] if row.get("type") == "llm_usage"]

        def fmt(v: float | None) -> str:
            return f"{v:.0f}ms" if v is not None else "n/a"

        def tokens(field: str) -> str:
            # No usage event means the count is unknown, not zero — "in=0 out=0"
            # would read as a free call. Same "n/a" as the latency fields.
            return str(sum(row[field] for row in llm)) if llm else "n/a"

        print(
            f"[METRICS] call={summary['call_id']} turns={summary['turns']} "
            f"EOU->audio p50={fmt(resp['p50'])} p95={fmt(resp['p95'])} | "
            f"LLM TTFT p50={fmt(pct['llm_ttft_ms']['p50'])} "
            f"TTS TTFB p50={fmt(pct['tts_ttfb_ms']['p50'])} | "
            f"LLM tokens in={tokens('input_tokens')} out={tokens('output_tokens')}"
        )
