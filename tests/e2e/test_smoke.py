"""Phase 1 smoke tests for the test-bus skeleton.

Goal: prove the daemon → shim → hardware loop is intact end-to-end. We do
NOT assume any specific module is loaded — assertions cover only behaviors
the stock shim guarantees:

  * the daemon answers PING with its version,
  * WAIT_FRAME advances the shim counter,
  * SNAPSHOT_PAD_LEDS returns 32 bytes,
  * a pad press round-trip flips at least one byte in the pad LED snapshot.

If `test_pad_press_changes_led` fails it means the press never reached the
firmware, the firmware never processed it, or the LED feedback path is
broken — all are real regressions worth catching.
"""

from __future__ import annotations

import pytest


def test_ping(bus):
    reply = bus.ping()
    assert reply.startswith("schwung-testd "), f"unexpected PING reply: {reply!r}"


def test_wait_frame_advances_counter(bus):
    a = bus.wait_frame(1).counter
    b = bus.wait_frame(2).counter
    # b should be at least 2 frames past a (counter is monotonic per frame)
    assert (b - a) >= 2, f"counter only advanced {b - a} in 2 waits"


def test_snapshot_pad_leds_returns_32_bytes(bus):
    snap = bus.snapshot_pad_leds()
    assert len(snap) == 32


def test_pad_press_changes_led(bus):
    """Pressing a pad on stock Move turns the pad LED on."""
    note = 84  # arbitrary mid-grid pad (index 16 in pad_led_colors)
    idx = bus.pad_index(note)

    # Baseline: settle a couple of frames, snapshot.
    bus.wait_frame(2)
    before = bus.snapshot_pad_leds()

    bus.press_pad(note, velocity=100)
    # The Move LED feedback path is firmware-driven; even on stock Move the
    # pad lights up almost immediately. Allow some headroom for JS modules
    # that may delay LED emission.
    bus.wait_frame(8)
    after_press = bus.snapshot_pad_leds()

    bus.release_pad(note)
    bus.wait_frame(8)
    after_release = bus.snapshot_pad_leds()

    if after_press == before:
        pytest.fail(
            f"pad {note} press did not change any LED state "
            f"(baseline={before.hex()})"
        )

    # Ideally the specific pad changed, but we tolerate a stricter assertion
    # at the indexed position only when the byte was zero before. If the
    # currently-loaded module already lit this pad, the test still passes
    # via the diff above.
    if before[idx] == 0:
        assert after_press[idx] != 0, (
            f"pad {note} (idx {idx}) was off before press, expected non-zero "
            f"after press; got {after_press[idx]}"
        )
