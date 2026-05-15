"""Pytest entry point for pytest-schwung.

Provides one fixture for the v1 skeleton: ``bus``, a session-scoped
``SchwungBus`` connected to the daemon (default 127.0.0.1:47777, override
with SCHWUNG_TEST_HOST / SCHWUNG_TEST_PORT). The bus auto-skips the
collected tests if the daemon is unreachable, so smoke tests degrade
gracefully on machines without a Move attached.
"""

from __future__ import annotations

import socket

import pytest

from .client import SchwungBus, SchwungBusError


@pytest.fixture(scope="session")
def bus() -> SchwungBus:
    b = SchwungBus()
    try:
        b.connect()
        b.ping()  # confirm protocol handshake works, not just TCP accept
    except (OSError, socket.timeout, SchwungBusError) as e:
        pytest.skip(
            f"schwung-testd unreachable at {b.host}:{b.port} ({e}). "
            "Start the daemon on Move and tunnel the port."
        )
    yield b
    b.close()
