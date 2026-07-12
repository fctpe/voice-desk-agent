"""CLI entry: ``python -m voice_desk <console|dev|start|connect>``.

Runs a friendly pre-flight before handing off to LiveKit's runner, so a keyless
console run prints one clear line ("set OPENAI_API_KEY") instead of failing deep
inside a connection attempt.
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

# Subcommands that actually boot the agent (and therefore need credentials).
_RUN_COMMANDS = {"console", "dev", "start", "connect"}


def _preflight() -> None:
    from .config import Settings

    settings = Settings.from_env()
    missing = settings.missing_keys()
    if not missing:
        return

    keys = ", ".join(missing)
    print(
        f"voice-desk-agent: missing required credentials for the '{settings.stack}' stack: "
        f"{keys}.\n"
        "  1. Copy .env.example to .env\n"
        "  2. Fill in the key(s) above\n"
        "For the zero-friction console demo you only need OPENAI_API_KEY "
        "(VOICE_DESK_STACK=openai, the default).",
        file=sys.stderr,
    )
    raise SystemExit(1)


def main() -> None:
    load_dotenv(override=False)

    command = sys.argv[1] if len(sys.argv) > 1 else None
    if command in _RUN_COMMANDS:
        _preflight()

    # Import lazily so `--help` and the pre-flight don't pay the (heavy) plugin
    # import cost, and so a missing-key exit stays a clean message.
    from livekit.agents import WorkerOptions, cli

    from .agent import entrypoint, prewarm

    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))


if __name__ == "__main__":
    main()
