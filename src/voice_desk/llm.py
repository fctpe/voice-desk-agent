"""One place to build the agent LLM, so the console agent and the eval harness
use the same settings.

`parallel_tool_calls=False` and a low temperature are what make multi-step
tool use reliable in this booking flow. Evals proved the difference: with
parallel tool calls on, or on gpt-4o-mini, the model narrates fake
availability and "booked" confirmations instead of calling the tools. This
matches the official LiveKit frontdesk example's config (gpt-4.1, sequential
tool calls). See docs/adr/0005.
"""

from __future__ import annotations

from typing import Any

# The default is 3, but a single booking turn legitimately chains
# identify_caller -> check_availability -> book_appointment -> send_confirmation
# (four tool steps). At 3 the run is truncated mid-flow and a critical call
# (often the booking itself) is silently dropped — the root cause of flaky
# eval failures. 8 gives comfortable headroom for the longest flow.
MAX_TOOL_STEPS = 8


def build_agent_llm(model: str) -> Any:
    from livekit.plugins import openai

    # temperature 0 for the most deterministic tool-calling we can get; the low
    # setting plus sequential tool calls is what keeps multi-step booking stable.
    # A generous timeout + retries absorb transient OpenAI 5xx/timeouts so an
    # eval scenario is never failed by a network hiccup rather than the agent.
    return openai.LLM(
        model=model,
        parallel_tool_calls=False,
        temperature=0.0,
        timeout=60.0,
        max_retries=5,
    )
