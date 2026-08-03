"""The trace export contract with agent-behavior-evals.

`behaveval judge` reads a trace through `fromLiveKitEvents` in that project's
`src/trace.ts`, which pulls a fixed set of fields off each row and ignores the
rest. Nothing in this repo imports that reader, so the only thing holding the two
ends together is this test: it drives the real agent and the real tools with a
scripted LLM (no network, no key) and pins the exported rows to exactly the
fields the reader consumes.

Without it, the export could drift into a shape the judge silently reads as a
trace of empty events — which looks like a passing behavior.
"""

from __future__ import annotations

import sys
from pathlib import Path

# evals/ is a script directory, not a package; put it on the path before
# importing the exporter under test.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))

from livekit.agents import AgentSession  # noqa: E402

from run_evals import TRACE_ITEM_TYPES, trace_events  # noqa: E402
from test_tracing import ScriptedLLM  # noqa: E402
from voice_desk.config import Settings  # noqa: E402
from voice_desk.tools import DeskContext, FrontDeskAgent, build_desk_context  # noqa: E402

# Every key `fromLiveKitEvents` reads off an item, by row type.
ITEM_KEYS = {
    "message": {"role", "text_content"},
    "function_call": {"name", "arguments"},
    "function_call_output": {"name", "output"},
}


async def _history(tmp_path: Path, fixed_now) -> AgentSession:
    settings = Settings(outbox_dir=str(tmp_path / "outbox"))
    userdata = build_desk_context(settings, now=fixed_now)
    session: AgentSession[DeskContext] = AgentSession(
        llm=ScriptedLLM(), userdata=userdata, max_tool_steps=4
    )
    await session.start(agent=FrontDeskAgent())
    await session.run(user_input="anything free tomorrow morning?")
    return session


async def test_export_matches_the_livekit_adapter_contract(tmp_path, fixed_now):
    session = await _history(tmp_path, fixed_now)
    events = trace_events(session)
    await session.aclose()

    assert events, "a completed turn must produce trace events"
    ids = [e["id"] for e in events]
    # The judge cites these ids in its findings, so they have to be stable
    # handles, not positions in a list that a later export could renumber.
    assert all(ids) and len(set(ids)) == len(ids)

    for event in events:
        assert set(event) == {"id", "type", "ts", "item"}
        assert event["type"] in TRACE_ITEM_TYPES
        assert set(event["item"]) == ITEM_KEYS[event["type"]]

    def first(kind: str) -> dict:
        return next(e["item"] for e in events if e["type"] == kind)

    # The caller's own words, and first in the trace: absent from
    # RunResult.events, and the whole premise of a caller-identity behavior.
    assert first("message") == {
        "role": "user",
        "text_content": "anything free tomorrow morning?",
    }
    assert [e["item"]["role"] for e in events if e["type"] == "message"] == ["user", "assistant"]

    # The tool name and its arguments: what the behavior spec calls the evidence
    # ("the phone number argument passed to each mutating tool").
    call = first("function_call")
    assert call["name"] == "check_availability"
    assert '"day_preference": "tomorrow"' in call["arguments"]
    assert first("function_call_output")["output"]


async def test_export_drops_session_bookkeeping(tmp_path, fixed_now):
    """History also carries agent_handoff and agent_config_update items. The
    reader has no field for either, so they would land as empty events and pad
    the trace the judge pages through."""
    session = await _history(tmp_path, fixed_now)
    history_types = {item.type for item in session.history.items}
    events = trace_events(session)
    await session.aclose()

    assert {"agent_handoff", "agent_config_update"} <= history_types
    assert {e["type"] for e in events} == set(TRACE_ITEM_TYPES)
