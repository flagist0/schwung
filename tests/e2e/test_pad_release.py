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

    Settle window: 30 frames (~87 ms). Move holds the press-glow color
    for ~15-25 frames after note-off before fading. If a future Move
    firmware lengthens that fade, bump the wait and re-measure.
    """
    note = 86  # mid-grid pad, away from typical step-pattern hot spots
    idx = bus.pad_index(note)

    bus.wait_frame(4)
    before = bus.snapshot_pad_leds()

    bus.press_pad(note, velocity=100)
    bus.wait_frame(8)
    pressed = bus.snapshot_pad_leds()

    if pressed[idx] == before[idx]:
        pytest.skip(
            f"pad {note} press did not change LED color "
            f"(before/pressed both {pressed[idx]}); release-path "
            "assertion is only meaningful if press visibly fired."
        )
    press_glow_color = pressed[idx]

    bus.release_pad(note)
    bus.wait_frame(30)
    released = bus.snapshot_pad_leds()

    assert released[idx] != press_glow_color, (
        f"pad {note} stuck at the press-glow color {press_glow_color} "
        f"30 frames after release. This is the classic 'stuck pad LED' "
        f"regression — a JS module or shim handler is missing the "
        f"note-off LED update.\n"
        f"  before:  {before.hex()}\n"
        f"  pressed: {pressed.hex()}\n"
        f"  release: {released.hex()}"
    )
