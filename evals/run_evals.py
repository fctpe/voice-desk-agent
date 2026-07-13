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
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Make `voice_desk` importable when run as a script from the repo root.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
SMOKE_COUNT = 3


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

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.passed for c in self.checks)


def _tool_names(result: Any) -> list[str]:
    from livekit.agents import FunctionCallEvent

    return [ev.item.name for ev in result.events if isinstance(ev, FunctionCallEvent)]


async def run_scenario(scenario: Scenario) -> ScenarioResult:
    """Drive one scenario end-to-end and collect pass/fail checks."""
    from livekit.agents import AgentSession

    from voice_desk.config import Settings
    from voice_desk.llm import MAX_TOOL_STEPS, build_agent_llm
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


async def run_all(scenarios: list[Scenario]) -> list[ScenarioResult]:
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        print(f"  running {scenario.id} ...", flush=True)
        results.append(await run_scenario(scenario))
    return results


async def run_reliability(scenarios: list[Scenario], runs: int) -> dict[str, list[bool]]:
    """Run every scenario `runs` times; return per-scenario pass/fail history so
    the reliability of a non-deterministic LLM agent can be measured, not
    assumed. This is the honest way to report a voice agent's pass rate."""
    history: dict[str, list[bool]] = {s.id: [] for s in scenarios}
    for run in range(1, runs + 1):
        print(f"— run {run}/{runs} —", flush=True)
        for scenario in scenarios:
            result = await run_scenario(scenario)
            history[scenario.id].append(result.passed)
            print(f"  {scenario.id:<24} {'pass' if result.passed else 'FAIL'}", flush=True)
    return history


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


def _write_json(results: list[ScenarioResult]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": time.time(),
        "passed": sum(1 for r in results if r.passed),
        "total": len(results),
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
    path = RESULTS_DIR / "evals.json"
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

    if not os.getenv("OPENAI_API_KEY"):
        print(
            "OPENAI_API_KEY is required to run evals (the agent LLM and the judge use it).\n"
            "Use --list to inspect scenarios without any API calls.",
            file=sys.stderr,
        )
        return 1

    if args.runs > 1:
        history = asyncio.run(run_reliability(scenarios, args.runs))
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
        out = RESULTS_DIR / "reliability.json"
        out.write_text(
            json.dumps(
                {
                    "runs": args.runs,
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

    results = asyncio.run(run_all(scenarios))
    _print_table(results)
    path = _write_json(results)
    print(f"wrote {path}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
