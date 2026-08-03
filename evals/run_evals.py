#!/usr/bin/env python3
"""Text-mode evaluation harness for the front-desk agent.

Built on LiveKit Agents' native testing API (the differentiator over bolt-on
audio-simulation SaaS): each scenario drives the *real* agent + tools through
``AgentSession.run(user_input=...)`` with no audio. Two kinds of check per
scenario: (1) which tools the agent actually called (cumulative across the
turn), asserted against the captured event stream; (2) an LLM judge over the
whole transcript for task-completion intents. The calendar/contacts are the
deterministic in-memory fakes, so only the model and the judge need a key.

Because a real LLM agent is not perfectly deterministic, ``--runs N`` runs every
scenario N times and reports per-scenario reliability — the honest way to state
a voice agent's pass rate.

Usage:
    python evals/run_evals.py --list      # print scenarios, ZERO LLM calls
    python evals/run_evals.py --smoke     # first 3 scenarios
    python evals/run_evals.py             # all scenarios, one pass
    python evals/run_evals.py --runs 5    # reliability: 5 passes per scenario
    python evals/run_evals.py --scenario emergency_escalation
    python evals/run_evals.py --scenario cross_caller_cancel --export-trace
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

# Make `voice_desk` importable when run as a script from the repo root.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
TRACE_DIR = Path(__file__).resolve().parent / "traces"
SMOKE_COUNT = 3

# The chat items a trace carries. `agent_handoff` and `agent_config_update` are
# session bookkeeping, not conduct, and the reader on the other end has no field
# for them — emitting them would only pad the trace with empty events.
TRACE_ITEM_TYPES = ("message", "function_call", "function_call_output")


@dataclass
class TurnSpec:
    user: str
    expect_tools: list[str] = field(default_factory=list)
    judge: str | None = None


@dataclass
class Scenario:
    id: str
    title: str
    description: str
    turns: list[TurnSpec]
    expect_tools: list[str] = field(default_factory=list)
    forbid_tools: list[str] = field(default_factory=list)
    final_judge: str | None = None
    source: str = ""


def load_scenarios() -> list[Scenario]:
    scenarios: list[Scenario] = []
    for path in sorted(SCENARIO_DIR.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        turns = [
            TurnSpec(
                user=t["user"],
                expect_tools=list(t.get("expect_tools", [])),
                judge=t.get("judge"),
            )
            for t in raw.get("turns", [])
        ]
        scenarios.append(
            Scenario(
                id=raw["id"],
                title=raw["title"],
                description=raw.get("description", ""),
                turns=turns,
                expect_tools=list(raw.get("expect_tools", [])),
                forbid_tools=list(raw.get("forbid_tools", [])),
                final_judge=raw.get("final_judge"),
                source=path.name,
            )
        )
    return scenarios


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ScenarioResult:
    id: str
    title: str
    checks: list[Check] = field(default_factory=list)
    error: str | None = None
    # LLM time-to-first-token per agent turn, from `ChatMessage.metrics` on each
    # assistant message. Text mode wires no STT/TTS, so this is the only stage of
    # the response budget the eval run can honestly measure.
    llm_ttft_ms: list[float] = field(default_factory=list)
    trace_path: Path | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.passed for c in self.checks)


def _tool_names(result: Any) -> list[str]:
    from livekit.agents import FunctionCallEvent

    return [ev.item.name for ev in result.events if isinstance(ev, FunctionCallEvent)]


async def run_scenario(scenario: Scenario, *, trace_dir: Path | None = None) -> ScenarioResult:
    """Drive one scenario end-to-end and collect pass/fail checks.

    With ``trace_dir`` set, the session's chat history is also written out as a
    behavior trace (see :func:`_write_trace`).
    """
    from livekit.agents import AgentSession

    from voice_desk.config import Settings
    from voice_desk.llm import MAX_TOOL_STEPS, build_agent_llm
    from voice_desk.metrics import CallMetrics
    from voice_desk.tools import DeskContext, FrontDeskAgent, build_desk_context

    settings = Settings.from_env()
    userdata = build_desk_context(settings)
    # Text-mode session: only an LLM is wired (no STT/TTS/VAD), so `session.run`
    # drives the real agent + tools over text. max_tool_steps must match the
    # runtime agent so the eval exercises the same multi-tool turns.
    session: AgentSession[DeskContext] = AgentSession(
        llm=build_agent_llm(settings.llm_model),
        userdata=userdata,
        max_tool_steps=MAX_TOOL_STEPS,
    )

    # Same collector the live agent uses, so eval latency and call latency are
    # measured by one code path. write=False: per-scenario files would be noise.
    call_metrics = CallMetrics(call_id=scenario.id)
    call_metrics.attach(session)

    result_obj = ScenarioResult(id=scenario.id, title=scenario.title)
    all_calls: list[str] = []
    expected_so_far: Counter[str] = Counter()
    last_result: Any = None

    try:
        await session.start(agent=FrontDeskAgent())
        for turn in scenario.turns:
            last_result = await session.run(user_input=turn.user)
            calls_this_turn = _tool_names(last_result)
            all_calls.extend(calls_this_turn)

            # Cumulative but count-aware: a confirming agent may pace a step a
            # turn earlier or later than the script, so we check "called at least
            # as many times as expected by this point", not "on this exact turn".
            # Counting (not membership) means a scenario expecting the same tool
            # on two turns still fails if the agent only calls it once.
            actual = Counter(all_calls)
            for tool in turn.expect_tools:
                expected_so_far[tool] += 1
                need = expected_so_far[tool]
                ok = actual[tool] >= need
                result_obj.checks.append(
                    Check(
                        name=f"has called {tool} x{need} by this turn",
                        passed=ok,
                        detail="" if ok else f"{tool} called {actual[tool]}x, need {need}",
                    )
                )

            if turn.judge:
                # Judge the conversation so far, not just this one message.
                result_obj.checks.append(await _judge_conversation(session, turn.judge))

        # Scenario-level tool expectations (anywhere in the call).
        for tool in scenario.expect_tools:
            result_obj.checks.append(
                Check(
                    name=f"calls {tool}",
                    passed=tool in all_calls,
                    detail="" if tool in all_calls else f"calls seen: {all_calls}",
                )
            )
        for tool in scenario.forbid_tools:
            result_obj.checks.append(
                Check(
                    name=f"does not call {tool}",
                    passed=tool not in all_calls,
                    detail="" if tool not in all_calls else "was called",
                )
            )

        if scenario.final_judge and last_result is not None:
            # Conversation-level: the framework's per-message judge sees only one
            # message with no context, so a task-completion intent ("confirmed a
            # concrete date and time") fails when the detail was stated a turn
            # earlier. Judge the whole transcript instead.
            result_obj.checks.append(await _judge_conversation(session, scenario.final_judge))
    except Exception as exc:  # noqa: BLE001 - surface any harness/runtime error as a failure
        result_obj.error = f"{type(exc).__name__}: {exc}"
    finally:
        report = call_metrics.finalize(write=False, echo=False)
        result_obj.llm_ttft_ms = [
            turn["llm_ttft_ms"] for turn in report["turns"] if turn["llm_ttft_ms"] is not None
        ]
        if trace_dir is not None:
            # In the finally and before aclose(), so a scenario that blew up
            # still leaves the trace of how far it got. The provenance block
            # records that it failed, so a partial trace cannot pass itself off
            # as a clean run.
            result_obj.trace_path = _write_trace(scenario, session, result_obj, trace_dir)
        try:
            await session.aclose()
        except Exception:  # pragma: no cover - teardown best-effort
            pass

    return result_obj


def _transcript(session: Any) -> str:
    """Flatten the session chat history into a readable transcript for the
    conversation-level judge."""
    lines: list[str] = []
    for item in session.history.items:
        role = getattr(item, "role", None)
        if role not in ("user", "assistant"):
            continue
        text = getattr(item, "text_content", None) or ""
        if text.strip():
            lines.append(f"{role.upper()}: {text.strip()}")
    return "\n".join(lines)


def trace_events(session: Any) -> list[dict[str, Any]]:
    """Flatten the session's chat history into the flat event list that
    agent-behavior-evals' LiveKit adapter reads.

    The shape is not ours to choose: ``fromLiveKitEvents`` in that project's
    ``src/trace.ts`` looks for ``id`` / ``type`` / ``ts`` on the row and
    ``role`` / ``text_content`` / ``name`` / ``arguments`` / ``output`` on
    ``item``. Nothing else is read, so nothing else is emitted — a field the
    reader ignores is a field that lies about being part of the contract.

    Built from ``session.history`` rather than the per-turn ``RunResult.events``
    because history is the whole call in one ordered list, and because
    ``RunResult`` only records what the *agent* produced: the caller's own turns
    are absent from it, and "which number did the caller claim" is exactly what
    a caller-identity behavior turns on.
    """
    events: list[dict[str, Any]] = []
    for item in session.history.items:
        if item.type not in TRACE_ITEM_TYPES:
            continue
        if item.type == "message":
            payload: dict[str, Any] = {"role": item.role, "text_content": item.text_content or ""}
        elif item.type == "function_call":
            payload = {"name": item.name, "arguments": item.arguments}
        else:
            payload = {"name": item.name, "output": item.output}
        events.append(
            {
                "id": item.id,
                "type": item.type,
                "ts": datetime.fromtimestamp(item.created_at, UTC).isoformat(
                    timespec="milliseconds"
                ),
                "item": payload,
            }
        )
    return events


def _write_trace(scenario: Scenario, session: Any, result: ScenarioResult, out_dir: Path) -> Path:
    """Write one scenario's trace for `behaveval judge` to score.

    The provenance block rides alongside ``events`` — the reader ignores keys it
    does not know — so a committed trace always says which model, which prompt
    and which commit produced it. A trace whose origin cannot be stated is not
    evidence of anything.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{scenario.id}.trace.json"
    payload = {
        "provenance": {
            **_provenance(),
            "scenario": scenario.id,
            "scenario_source": scenario.source,
            "scenario_passed": result.passed,
        },
        "events": trace_events(session),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


async def _judge_conversation(session: Any, intent: str) -> Check:
    """Judge whether the whole conversation satisfied an intent. Uses the OpenAI
    API directly with a strict rubric so the judge sees full context (the
    framework's judge() is single-message only)."""
    from openai import AsyncOpenAI

    from voice_desk.config import Settings

    transcript = _transcript(session)
    prompt = (
        "You are a strict evaluator. Read the phone-call transcript between a "
        "caller (USER) and an AI front desk (ASSISTANT). Decide whether the "
        "ASSISTANT, across the whole conversation, satisfied this intent:\n\n"
        f"INTENT: {intent}\n\n"
        f"TRANSCRIPT:\n{transcript}\n\n"
        'Reply with JSON only: {"pass": true|false, "reason": "one sentence"}.'
    )
    client = AsyncOpenAI()
    name = f"conversation: {intent[:60]}"
    try:
        resp = await client.chat.completions.create(
            model=Settings.from_env().judge_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            response_format={"type": "json_object"},
        )
        verdict = json.loads(resp.choices[0].message.content or "{}")
        passed = bool(verdict.get("pass"))
        detail = "" if passed else str(verdict.get("reason", ""))[:200]
        return Check(name=name, passed=passed, detail=detail)
    except Exception as exc:  # noqa: BLE001
        return Check(name=name, passed=False, detail=f"judge error: {exc}")
    finally:
        await client.close()


async def run_all(
    scenarios: list[Scenario], *, trace_dir: Path | None = None
) -> list[ScenarioResult]:
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        print(f"  running {scenario.id} ...", flush=True)
        results.append(await run_scenario(scenario, trace_dir=trace_dir))
    return results


async def run_reliability(
    scenarios: list[Scenario], runs: int
) -> tuple[dict[str, list[bool]], list[ScenarioResult]]:
    """Run every scenario `runs` times; return per-scenario pass/fail history so
    the reliability of a non-deterministic LLM agent can be measured, not
    assumed. This is the honest way to report a voice agent's pass rate.

    Also returns every result, because a reliability sweep is by far the largest
    honest latency sample the harness ever collects.
    """
    history: dict[str, list[bool]] = {s.id: [] for s in scenarios}
    every: list[ScenarioResult] = []
    for run in range(1, runs + 1):
        print(f"— run {run}/{runs} —", flush=True)
        for scenario in scenarios:
            result = await run_scenario(scenario)
            history[scenario.id].append(result.passed)
            every.append(result)
            print(f"  {scenario.id:<24} {'pass' if result.passed else 'FAIL'}", flush=True)
    return history, every


def _print_table(results: list[ScenarioResult]) -> None:
    width = max((len(r.id) for r in results), default=10)
    print("\n" + "=" * (width + 40))
    print(f"{'SCENARIO':<{width}}  RESULT   CHECKS")
    print("-" * (width + 40))
    for r in results:
        ok = sum(1 for c in r.checks if c.passed)
        total = len(r.checks)
        status = "PASS" if r.passed else "FAIL"
        print(f"{r.id:<{width}}  {status:<7}  {ok}/{total}")
        if r.error:
            print(f"{'':<{width}}    error: {r.error}")
        for c in r.checks:
            if not c.passed:
                print(f"{'':<{width}}    x {c.name}: {c.detail}")
    passed = sum(1 for r in results if r.passed)
    print("-" * (width + 40))
    print(f"TOTAL: {passed}/{len(results)} scenarios passed")
    print("=" * (width + 40))


def _git_sha() -> str | None:
    """Short SHA of the tree that produced these numbers, so a committed result
    can always be traced back to the code that made it."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git present
        return None
    return out.stdout.strip() or None


def _provenance() -> dict[str, Any]:
    from voice_desk.config import Settings
    from voice_desk.llm import MAX_TOOL_STEPS
    from voice_desk.prompts import SYSTEM_PROMPT

    settings = Settings.from_env()
    return {
        "generated_at": time.time(),
        "generated_at_iso": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "model": settings.llm_model,
        "judge_model": settings.judge_model,
        "max_tool_steps": MAX_TOOL_STEPS,
        # The prompt is the single biggest lever on these numbers; hash it so a
        # silent prompt edit can never masquerade as the same measurement.
        "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:16],
    }


def _latency_block(results: list[ScenarioResult]) -> dict[str, Any]:
    """LLM TTFT across every agent turn in this run.

    Text mode wires no STT/TTS, so this covers the LLM stage only — the largest
    stage of the response budget and the one the model choice controls.
    """
    from voice_desk.metrics import percentile

    samples = [ms for r in results for ms in r.llm_ttft_ms]
    return {
        "stage": "llm_ttft_ms",
        # Stated in full because the definition changed with the move off
        # `metrics_collected` (ADR-0007): a tool-calling turn makes several model
        # calls, and this is the one that produced the reply text, not the first.
        "note": (
            "TTFT of the model call that produced the turn's reply text; "
            "LLM stage only, text-mode evals wire no STT/TTS."
        ),
        "turns_measured": len(samples),
        "p50": percentile(samples, 50),
        "p95": percentile(samples, 95),
    }


def _write_json(results: list[ScenarioResult], *, partial: bool) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        **_provenance(),
        "passed": sum(1 for r in results if r.passed),
        "total": len(results),
        "latency": _latency_block(results),
        "scenarios": [
            {
                "id": r.id,
                "title": r.title,
                "passed": r.passed,
                "error": r.error,
                "checks": [
                    {"name": c.name, "passed": c.passed, "detail": c.detail} for c in r.checks
                ],
            }
            for r in results
        ],
    }
    # A subset run must never overwrite the committed full-run artifact. CI runs
    # --smoke; without this, three green scenarios would replace the ten-scenario
    # result the README cites, and nothing would flag it.
    path = RESULTS_DIR / ("evals.partial.json" if partial else "evals.json")
    payload["partial"] = partial
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run front-desk agent evals (text mode).")
    parser.add_argument("--list", action="store_true", help="List scenarios (no LLM calls).")
    parser.add_argument("--smoke", action="store_true", help=f"Run first {SMOKE_COUNT} scenarios.")
    parser.add_argument("--scenario", help="Run a single scenario by id.")
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Run every scenario N times and report per-scenario reliability. "
        "A real LLM agent is not perfectly deterministic; use this to measure it.",
    )
    parser.add_argument(
        "--export-trace",
        action="store_true",
        help=f"Also write each scenario's trace to {TRACE_DIR.name}/<id>.trace.json for "
        "`behaveval judge` to score against .agents/behaviors/.",
    )
    args = parser.parse_args()

    scenarios = load_scenarios()

    if args.list:
        print(f"{len(scenarios)} scenarios:")
        for i, s in enumerate(scenarios, start=1):
            print(f"  {i:>2}. {s.id:<24} {s.title}")
        return 0

    if args.scenario:
        scenarios = [s for s in scenarios if s.id == args.scenario]
        if not scenarios:
            print(f"no scenario with id '{args.scenario}'", file=sys.stderr)
            return 2
    elif args.smoke:
        scenarios = scenarios[:SMOKE_COUNT]

    if args.export_trace and args.runs > 1:
        # One path per scenario, so run N would leave only the last pass and
        # silently call it "the" trace. Refuse rather than overwrite.
        print(
            "--export-trace writes one trace per scenario; it cannot be used with --runs > 1.",
            file=sys.stderr,
        )
        return 2

    if not os.getenv("OPENAI_API_KEY"):
        print(
            "OPENAI_API_KEY is required to run evals (the agent LLM and the judge use it).\n"
            "Use --list to inspect scenarios without any API calls.",
            file=sys.stderr,
        )
        return 1

    if args.runs > 1:
        history, every_result = asyncio.run(run_reliability(scenarios, args.runs))
        total_pass = sum(sum(h) for h in history.values())
        total = sum(len(h) for h in history.values())
        print(f"\n{'SCENARIO':<24} RELIABILITY")
        print("-" * 40)
        for sid, hist in history.items():
            print(f"{sid:<24} {sum(hist)}/{len(hist)}")
        print("-" * 40)
        rate = total_pass / total if total else 0.0
        print(f"aggregate: {total_pass}/{total} scenario-runs passed ({rate:.0%})")
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        # Same rule as the single-pass path: a subset sweep must not overwrite
        # the committed artifact the README cites. --scenario X --runs 5 is the
        # normal way to chase one flaky scenario, and it used to clobber the
        # whole-suite result with a one-scenario number.
        partial = bool(args.smoke or args.scenario)
        out = RESULTS_DIR / ("reliability.partial.json" if partial else "reliability.json")
        out.write_text(
            json.dumps(
                {
                    **_provenance(),
                    "partial": partial,
                    "runs": args.runs,
                    "latency": _latency_block(every_result),
                    "per_scenario": {
                        sid: {"passed": sum(h), "of": len(h)} for sid, h in history.items()
                    },
                    "aggregate_passed": total_pass,
                    "aggregate_of": total,
                },
                indent=2,
            )
        )
        print(f"wrote {out}")
        return 0 if total_pass == total else 1

    results = asyncio.run(run_all(scenarios, trace_dir=TRACE_DIR if args.export_trace else None))
    _print_table(results)
    path = _write_json(results, partial=bool(args.smoke or args.scenario))
    print(f"wrote {path}")
    for r in results:
        if r.trace_path is not None:
            print(f"wrote {r.trace_path}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
