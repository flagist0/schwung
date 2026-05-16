"""Four-on-the-floor UI regression test.

The user-described scenario from the test-bus design discussion: set
steps 1/5/9/13 on Move's sequencer for the current track, verify the
step LEDs light up exactly where we put them, then undo on teardown.

Preconditions (test SKIPs if not met — author must position the device):
  - Move in note-edit mode (move_ui_mode == 2). This is the screen where
    step pads are sequencer toggles, not playback triggers.
  - All four target steps (1, 5, 9, 13) start UNLIT. If they were
    already lit, the test would be a no-op (toggle off then on at
    teardown), so we skip rather than do something misleading.

SAFETY (silent test): in note-edit mode with transport STOPPED, toggling
a step does not play audio. With transport playing, the newly-added
note will sound when the playhead reaches it. Run with transport
stopped (or headphones, per current dev setup).
"""

from __future__ import annotations

import pytest

from schwung_bus import BusState
from schwung_bus.move_commands import ToggleStep


# Steps 1, 5, 9, 13 = downbeats in a 16-step bar at 1/16 resolution.
FOUR_ON_THE_FLOOR_STEPS = (1, 5, 9, 13)


def test_four_on_the_floor_lights_correct_step_leds(bus, commander):
    """Toggle 1/5/9/13 → verify LEDs lit exactly at those positions,
    others stay dark; commander undo at teardown clears them back."""

    # ---- Mode precondition ----
    state = bus.state()
    if state.move_ui_mode != BusState.MOVE_UI_NOTE:
        pytest.skip(
            f"Move must be in note-edit mode for this test "
            f"(move_ui_mode={state.move_ui_mode}, want=2). "
            "Press 'Note' on the device and pick a track, then re-run."
        )
    if state.transport_playing:
        pytest.skip(
            "Transport is playing — toggling steps would sound. "
            "Stop transport before re-running this test."
        )

    # ---- LED-state precondition: target steps must start dark ----
    initial = bus.snapshot_step_leds()
    for step in FOUR_ON_THE_FLOOR_STEPS:
        idx = step - 1
        if initial[idx] != 0:
            pytest.skip(
                f"step {step} (LED idx {idx}) is already lit "
                f"(color={initial[idx]}); test would no-op + un-toggle "
                "at teardown which is misleading. Clear the pattern first."
            )

    # ---- Execute: toggle each downbeat through commander ----
    for step in FOUR_ON_THE_FLOOR_STEPS:
        commander.do(ToggleStep(step))

    # Give Move a couple of SPI frames to propagate the LED changes
    # back through the led_queue and into the overlay SHM.
    bus.wait_frame(8)

    # ---- Assertion: the four downbeats are now lit, others unchanged ----
    after = bus.snapshot_step_leds()

    lit_after  = {i + 1 for i in range(16) if after[i] != 0}
    lit_before = {i + 1 for i in range(16) if initial[i] != 0}
    newly_lit  = lit_after - lit_before

    expected_newly_lit = set(FOUR_ON_THE_FLOOR_STEPS)
    assert newly_lit == expected_newly_lit, (
        f"expected newly-lit steps {sorted(expected_newly_lit)}, "
        f"got {sorted(newly_lit)}. "
        f"Before: {initial.hex()}; After: {after.hex()}"
    )

    # Specifically verify that step 2 (between downbeats) didn't get
    # set as a side effect — i.e. the LED mapping is per-step exact,
    # not per-pad-row or anything coarser. (step 2 = idx 1.)
    if lit_before[1] if 1 < len(lit_before) else False:
        pass  # was already lit; skip this specific check
    else:
        assert after[1] == 0, (
            f"step 2 should still be dark after only toggling 1/5/9/13, "
            f"got color={after[1]}"
        )

    # commander.undo_all() in fixture teardown will press 13/9/5/1
    # again (LIFO) to toggle them off.
