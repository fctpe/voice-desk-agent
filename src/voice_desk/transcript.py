"""Per-call transcript, written as JSON Lines.

One object per conversation item (``{"ts", "role", "text"}``), appended as the
call unfolds so a crash still leaves a partial record. Sourced from the
session's ``conversation_item_added`` event; also usable directly in tests.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class CallTranscript:
    """Collects and persists a call's turns to ``transcript-<call_id>.jsonl``."""

    def __init__(self, *, call_id: str, transcript_dir: str | Path = "transcripts") -> None:
        self.call_id = call_id
        self._dir = Path(transcript_dir)
        self._entries: list[dict[str, Any]] = []
        self._path: Path | None = None

    @property
    def entries(self) -> list[dict[str, Any]]:
        return list(self._entries)

    @property
    def path(self) -> Path:
        return self._dir / f"transcript-{self.call_id}.jsonl"

    def attach(self, session: Any) -> None:
        """Subscribe to a live AgentSession's conversation stream."""

        @session.on("conversation_item_added")
        def _on_item(ev: Any) -> None:  # pragma: no cover - exercised in live runs
            item = getattr(ev, "item", None)
            role = getattr(item, "role", None)
            text = getattr(item, "text_content", None)
            if role and text:
                self.add(role, text)

    def add(self, role: str, text: str) -> None:
        entry = {"ts": round(time.time(), 3), "role": role, "text": text}
        self._entries.append(entry)
        self._append_line(entry)

    def _append_line(self, entry: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def finalize(self) -> Path:
        """Ensure the file exists (even for an empty call) and return its path."""
        if not self.path.exists():
            self._dir.mkdir(parents=True, exist_ok=True)
            self.path.touch()
        return self.path
