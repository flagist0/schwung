"""Phase 3 L2 reset — restart-move.sh-based fixture smoke test.

Exercises the ``fresh_move`` fixture (full Move firmware restart via
shim's restart_move flag) end-to-end on real hardware. Verifies the
~3-second freeze-then-thaw cycle works through the Python client.

This test is slower than other smoke tests (~3-5 s per run including
fixture) but proves the fast-reset mechanism we'll build pristine_set
on top of.

SAFETY: this test produces no audio (restart-move.sh is silent —
SIGTERM/SIGKILL + relaunch). Move's display briefly shows the firmware
restart screen.
"""

from __future__ import annotations

import time


def test_restart_move_then_wait_ready(bus):
    """Direct primitive test — call restart_move, wait for shim, confirm
    state is consistent. Doesn't use the fixture so timing is observable."""
    s_before = bus.state()

    t0 = time.monotonic()
    bus.restart_move()
    t1 = time.monotonic()
    assert (t1 - t0) < 0.5, f"restart_move() should return ~instantly, took {(t1-t0):.2f}s"

    counter_after_ready = bus.wait_for_shim_ready(timeout=15)
    t2 = time.monotonic()

    total = t2 - t0
    assert 1.0 < total < 10.0, (
        f"restart cycle outside expected 1-10 s envelope: {total:.2f}s "
        f"(was the shim restarted at all?)"
    )

    # Verify shim continues ticking — wait_frame blocks until it does
    # (or times out via the daemon's 30s cap). Avoids the race where
    # consecutive bus.state() calls finish faster than one ~3 ms frame.
    wf = bus.wait_frame(2)
    assert wf.counter > counter_after_ready, (
        f"counter should advance past wait_for_shim_ready value "
        f"({counter_after_ready} -> {wf.counter})"
    )

    # Restart preserves Move-native UI (no schwung overlay).
    s_after = bus.state()
    assert s_after.overtake_mode == 0


def test_fresh_move_fixture_round_trip(bus, fresh_move):
    """Fixture wraps restart_move + wait. After the fixture yields,
    Move should be freshly restarted and ticking."""
    s = bus.state()
    assert s.shim_counter > 0
    assert s.overtake_mode == 0


def test_fresh_move_then_state_probes_work(bus, fresh_move):
    """After a restart, the daemon's SHM-backed reads should all work
    normally — no stale mappings, no crashes."""
    assert bus.ping().startswith("schwung-testd")
    s = bus.state()
    pads = bus.snapshot_pad_leds()
    steps = bus.snapshot_step_leds()
    assert len(pads) == 32
    assert len(steps) == 16
    assert s.shim_counter > 0
