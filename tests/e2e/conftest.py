"""Conftest for schwung's own on-device E2E tests.

Tests live in ``tests/e2e/`` and require a running ``schwung-testd`` on the
target Move (default localhost:47777, tunneled via SSH from the dev machine).
The ``bus`` fixture comes from the ``pytest-schwung`` plugin (installed from
``tools/pytest-schwung``).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _bus_state_cleanup(bus):
    """Best-effort isolation between tests.

    After each test, emit one All-Notes-Off (CC 123, channel 0) and let a
    few SPI frames pass so the firmware processes it before the next test
    starts. Tests that pressed pads and never released them (or that hit
    an assertion mid-sequence) won't leak hanging notes into later tests.

    Tests that need deeper isolation (e.g. module reload, set switch)
    should add their own teardown — this is best-effort, not exhaustive.
    """
    yield
    try:
        # USB-MIDI packet for CC 123 on cable 0 channel 0: CIN=0xB, status=0xB0
        bus.inject_midi(bytes([0x0B, 0xB0, 123, 0]))
        bus.wait_frame(4)
    except Exception:
        # Cleanup must never fail a test in teardown; the failure that
        # caused the leak already has its own report.
        pass
