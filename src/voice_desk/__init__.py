"""voice-desk-agent — Riley, an AI voice front desk for field-service businesses.

Built on LiveKit Agents. The package is provider-agnostic: the voice pipeline
(STT / LLM / TTS) is selected from the environment so a single ``OPENAI_API_KEY``
is enough to run the console demo, while a Deepgram + Cartesia + OpenAI pipeline
is available for lower latency and per-stage cost control.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
