"""The network guard itself must actually block connections."""

from __future__ import annotations

import socket

import pytest


def test_outbound_connect_is_blocked():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(RuntimeError, match="network access blocked"):
        sock.connect(("example.com", 80))


def test_create_connection_is_blocked():
    with pytest.raises(RuntimeError, match="network access blocked"):
        socket.create_connection(("example.com", 80))
