"""Pad release-path regression test.

``test_smoke.test_pad_press_changes_led`` covers the press path. This
file closes the loop: after a press + release, the pad LED must leave
the brightened press-glow state. Exact baseline restoration is NOT
asserted — Move's firmware shifts nearby pads' brightness by ±1 byte
depending on which other pads are currently held, so byte-exact
restoration is flaky on stock hardware. The robust observable is:
"the pad is no longer in the brightest highlight color".

The motivating regression: a JS overtake module forgets to repaint
on note-off, leaving the pad stuck in its press-glow color until
something else triggers a refresh. This test catches that on the
stock firmware path; for JS-module-specific stuck-pad regressions,
add a sibling test that loads the module first.
"""

from __future__ import annotations

import pytest


def test_pad_release_clears_press_glow(bus):
    """Press a pad → it brightens. Release → that brightness goes away.

    Poll for up to ~60 frames (~175 ms) waiting for the press-glow
    to clear. Move normally clears within 15-25 frames, but under
    burst-load tests that run before this one the firmware seems
    to hold the highlight longer — the test isn't asserting on the
    exact timing, only that the LED EVENTUALLY leaves the press-
    glow color. A real stuck-pad regression keeps the LED at the
    press-glow color forever, which this still catches with the
    extended budget.
    """
    note = 86  # mid-grid pad, away from typical step-pattern hot spots
    idx = bus.pad_index(note)

    bus.wait_frame(4)
    before = bus.snapshot_pad_leds()

    bus.press_pad(note, velocity=100)
    bus.wait_frame(8)
    pressed = bus.snapshot_pad_leds()

    if pressed[idx] == before[idx]:
        bus.release_pad(note)
        pytest.skip(
            f"pad {note} press did not change LED color "
            f"(before/pressed both {pressed[idx]}); release-path "
            "assertion is only meaningful if press visibly fired."
        )
    press_glow_color = pressed[idx]

    bus.release_pad(note)
    # Poll every ~6 frames up to 60 — most clears land by frame 30
    # but burst-load residual state can extend the hold.
    for _ in range(10):
        bus.wait_frame(6)
        released = bus.snapshot_pad_leds()
        if released[idx] != press_glow_color:
            return  # cleared

    # Still at press-glow color after ~175 ms — real stuck-pad.
    pytest.fail(
        f"pad {note} stuck at the press-glow color {press_glow_color} "
        f"~175 ms after release. This is the classic 'stuck pad LED' "
        f"regression — a JS module or shim handler is missing the "
        f"note-off LED update.\n"
        f"  before:  {before.hex()}\n"
        f"  pressed: {pressed.hex()}\n"
        f"  release: {released.hex()}"
    )
