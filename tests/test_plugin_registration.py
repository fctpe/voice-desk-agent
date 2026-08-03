"""Every provider plugin must be registered by importing :mod:`voice_desk.agent`.

livekit-agents refuses to register a plugin from anything but the main thread,
and the entrypoint that builds the pipeline runs on a job thread — so a plugin
imported lazily inside the build helpers kills the call with "Plugins must be
registered on the main thread". Importing them at module scope is what makes the
registration happen on the main thread, and this pins it: the console demo is the
README's first command and no other test boots a real pipeline.
"""

from __future__ import annotations

from livekit.agents import Plugin

import voice_desk.agent  # noqa: F401 - imported for its registration side effect


def test_importing_the_agent_registers_every_provider_plugin() -> None:
    registered = {plugin.package for plugin in Plugin.registered_plugins}
    assert {
        "livekit.plugins.cartesia",
        "livekit.plugins.deepgram",
        "livekit.plugins.openai",
        "livekit.plugins.silero",
    } <= registered
