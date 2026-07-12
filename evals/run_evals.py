#!/usr/bin/env python3
"""Text-mode evaluation harness for the front-desk agent.

Built on LiveKit Agents' native testing API (the differentiator over bolt-on
audio-simulation SaaS): each scenario drives the *real* agent + tools through
``AgentSession.run(user_input=...)`` with no audio, then asserts on the captured
event stream (``result.expect.contains_function_call(...)``) and judges the
final reply with an LLM (``.judge(llm, intent=...)``). The calendar/contacts are
the deterministic in-memory fakes, so only the model and the judge need a key.

Usage:
    python evals/run_evals.py --list      # print scenarios, ZERO LLM calls
    python evals/run_evals.py --smoke     # first 3 scenarios
    python evals/run_evals.py             # all scenarios
    python evals/run_evals.py --scenario emergency_escalation
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
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


def _last_assistant_index(result: Any) -> int | None:
    from livekit.agents import ChatMessageEvent

    found: int | None = None
    for i, ev in enumerate(result.events):
        if isinstance(ev, ChatMessageEvent) and ev.item.role == "assistant":
            found = i
    return found


def _tool_names(result: Any) -> list[str]:
    from livekit.agents import FunctionCallEvent

    return [ev.item.name for ev in result.events if isinstance(ev, FunctionCallEvent)]


async def run_scenario(scenario: Scenario, judge_llm: Any) -> ScenarioResult:
    """Drive one scenario end-to-end and collect pass/fail checks."""
    from livekit.agents import AgentSession
    from livekit.plugins import openai

    from voice_desk.config import Settings
    from voice_desk.tools import DeskContext, FrontDeskAgent, build_desk_context

    settings = Settings.from_env()
    userdata = build_desk_context(settings)
    # Text-mode session: only an LLM is wired (no STT/TTS/VAD), so `session.run`
    # drives the real agent + tools over text.
    session: AgentSession[DeskContext] = AgentSession(
        llm=openai.LLM(model=settings.llm_model),
        userdata=userdata,
    )

    result_obj = ScenarioResult(id=scenario.id, title=scenario.title)
    all_calls: list[str] = []
    last_result: Any = None

    try:
        await session.start(agent=FrontDeskAgent())
        for turn in scenario.turns:
            last_result = await session.run(user_input=turn.user)
            calls_this_turn = _tool_names(last_result)
            all_calls.extend(calls_this_turn)

            for tool in turn.expect_tools:
                result_obj.checks.append(
                    Check(
                        name=f"turn calls {tool}",
                        passed=tool in calls_this_turn,
                        detail="" if tool in calls_this_turn else f"saw {calls_this_turn}",
                    )
                )

            if turn.judge:
                result_obj.checks.append(await _judge_last(last_result, judge_llm, turn.judge))

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
            result_obj.checks.append(
                await _judge_last(last_result, judge_llm, scenario.final_judge)
            )
    except Exception as exc:  # noqa: BLE001 - surface any harness/runtime error as a failure
        result_obj.error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            await session.aclose()
        except Exception:  # pragma: no cover - teardown best-effort
            pass

    return result_obj


async def _judge_last(result: Any, judge_llm: Any, intent: str) -> Check:
    idx = _last_assistant_index(result)
    if idx is None:
        return Check(name=f"judge: {intent}", passed=False, detail="no assistant message")
    try:
        await result.expect[idx].is_message(role="assistant").judge(judge_llm, intent=intent)
        return Check(name=f"judge: {intent}", passed=True)
    except AssertionError as exc:
        return Check(name=f"judge: {intent}", passed=False, detail=str(exc).splitlines()[0][:200])


async def run_all(scenarios: list[Scenario]) -> list[ScenarioResult]:
    from livekit.plugins import openai

    from voice_desk.config import Settings

    judge_llm = openai.LLM(model=Settings.from_env().llm_model)
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        print(f"  running {scenario.id} ...", flush=True)
        results.append(await run_scenario(scenario, judge_llm))
    return results


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

    results = asyncio.run(run_all(scenarios))
    _print_table(results)
    path = _write_json(results)
    print(f"wrote {path}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
