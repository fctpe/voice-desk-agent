"""CLI entry: ``python -m voice_desk <console|dev|start|connect>``.

Runs a friendly pre-flight before handing off to LiveKit's runner, so a run that
is missing a credential prints one clear line ("set OPENAI_API_KEY") instead of
failing deep inside a connection attempt.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from dotenv import load_dotenv

if TYPE_CHECKING:
    from .config import Settings

# Subcommands that actually boot the agent (and therefore need credentials).
_RUN_COMMANDS = {"console", "dev", "start", "connect"}

# Of those, the ones that reach a LiveKit server. The console runs the whole call
# locally, which is the point of the one-key quickstart.
_SERVER_COMMANDS = {"dev", "start", "connect"}


def _flag_given(flag: str) -> bool:
    return any(arg == flag or arg.startswith(f"{flag}=") for arg in sys.argv[1:])


def _missing_livekit(settings: Settings) -> list[str]:
    """LiveKit credentials that neither the environment nor the command line supplies.

    LiveKit's CLI takes each of these as a flag as well as an env var, so a run
    that passes the flag is not missing anything — checking the variable alone
    would reject a perfectly good invocation.
    """
    return [
        name
        for name, flag, value in (
            ("LIVEKIT_URL", "--url", settings.livekit_url),
            ("LIVEKIT_API_KEY", "--api-key", settings.livekit_api_key),
            ("LIVEKIT_API_SECRET", "--api-secret", settings.livekit_api_secret),
        )
        if not value and not _flag_given(flag)
    ]


def _fail(message: str) -> None:
    print(f"voice-desk-agent: {message}", file=sys.stderr)
    raise SystemExit(1)


def _preflight(command: str) -> None:
    from .config import Settings

    settings = Settings.from_env()
    if missing := settings.missing_keys():
        _fail(
            f"missing required credentials for the '{settings.stack}' stack: "
            f"{', '.join(missing)}.\n"
            "  1. Copy .env.example to .env\n"
            "  2. Fill in the key(s) above\n"
            "For the zero-friction console demo you only need OPENAI_API_KEY "
            "(VOICE_DESK_STACK=openai, the default)."
        )

    # Without these the worker dies inside LiveKit's connect with a traceback —
    # and an exit code of 0, so `make dev` reports success on a worker that never
    # ran. Refuse here instead, while we can still say what to do about it.
    if command in _SERVER_COMMANDS and (missing := _missing_livekit(settings)):
        _fail(
            f"'{command}' connects to a LiveKit server and needs: {', '.join(missing)}.\n"
            "  Take them from your LiveKit project settings and put them in .env\n"
            "  (or pass --url / --api-key / --api-secret).\n"
            "The console demo needs none of them: python -m voice_desk console."
        )


def main() -> None:
    load_dotenv(override=False)

    command = sys.argv[1] if len(sys.argv) > 1 else None
    if command in _RUN_COMMANDS:
        _preflight(command)

    # Import lazily so `--help` and the pre-flight don't pay the (heavy) plugin
    # import cost, and so a missing-key exit stays a clean message.
    from livekit.agents import WorkerOptions, cli

    from .agent import entrypoint, prewarm

    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))


if __name__ == "__main__":
    main()
