"""Shared fixtures, including a hard network guard.

Every unit test is meant to run fully offline (the providers are in-memory
fakes). The autouse ``_no_network`` fixture makes that a guarantee rather than a
hope: any outbound socket connect raises, so a test that accidentally reaches
for the network fails loudly instead of hitting a real API. Local socketpairs
(used by the asyncio event loop) still work, because only ``connect`` is blocked.
"""

from __future__ import annotations

import socket
from datetime import datetime

import pytest


class _NoNetworkSocket(socket.socket):
    def connect(self, address, *args, **kwargs):  # type: ignore[override]
        raise RuntimeError(f"network access blocked in unit tests (connect to {address!r})")

    def connect_ex(self, address, *args, **kwargs):  # type: ignore[override]
        raise RuntimeError(f"network access blocked in unit tests (connect_ex to {address!r})")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch):
    """Block outbound network connections for the duration of every test."""

    def _blocked_create_connection(*args, **kwargs):
        raise RuntimeError("network access blocked in unit tests (create_connection)")

    monkeypatch.setattr(socket, "socket", _NoNetworkSocket)
    monkeypatch.setattr(socket, "create_connection", _blocked_create_connection)
    yield


@pytest.fixture
def fixed_now() -> datetime:
    """A deterministic Monday 07:00 anchor so slot generation is reproducible."""
    return datetime(2026, 7, 13, 7, 0, 0)
