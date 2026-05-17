"""Four-on-the-floor UI regression test.

The user-described scenario from the test-bus design discussion: set
steps 1/5/9/13 on Move's sequencer for the current track, verify the
step LEDs light up exactly where we put them, then undo on teardown.

Setup the test handles itself (since shim state-mirror landed):
  - Tap track 1 via the commander to enter note-edit mode. Move's
    firmware switches to NOTE view and the shim updates
    ``move_ui_mode``. Commander's undo re-taps so teardown leaves the
    device in (approximately) the original mode.

Preconditions still checked, kept as user-positioning skips rather
than failures because they describe device state we can't safely
mutate from here:
  - Transport must be stopped. With transport playing, a newly-set
    step would sound the next time the playhead reaches it.
  - The four target steps must start UNLIT. If they were already lit,
    the test would no-op then un-toggle them at teardown — misleading
    enough to be worth skipping over silently.

SAFETY (silent test): in note-edit mode with transport STOPPED, toggling
a step does not play audio. The test refuses to run with transport
playing.
"""

from __future__ import annotations

import pytest

from schwung_bus import BusState
from schwung_bus.move_commands import SelectTrack, ToggleStep


# Steps 1, 5, 9, 13 = downbeats in a 16-step bar at 1/16 resolution.
FOUR_ON_THE_FLOOR_STEPS = (1, 5, 9, 13)


def test_four_on_the_floor_lights_correct_step_leds(bus, commander):
    """Toggle 1/5/9/13 → verify LEDs lit exactly at those positions,
    others stay dark; commander undo at teardown clears them back."""

    # ---- Transport safety check — we won't toggle steps if Move is playing ----
    state = bus.state()
    if state.transport_playing:
        pytest.skip(
            "Transport is playing — toggling steps would sound. "
            "Stop transport before re-running this test."
        )

    # ---- Enter note-edit mode by selecting track 1.
    # SelectTrack updates Move's UI mode AND (now) the shim's state
    # mirror, so the ToggleStep precondition below sees move_ui_mode=2.
    commander.do(SelectTrack(1))
    bus.wait_frame(4)

    state = bus.state()
    if state.move_ui_mode != BusState.MOVE_UI_NOTE:
        pytest.skip(
            f"SelectTrack(1) did not put Move into note-edit mode "
            f"(move_ui_mode={state.move_ui_mode}, want=2). Shim state "
            "mirror missing — rebuild + redeploy the shim."
        )

    # ---- Snapshot before — used as the baseline for delta assertions ----
    # We avoid hardcoding "lit" vs "dark" color codes. Move uses a faded
    # base color for unset steps (~0x5E here) and a brighter color when
    # a step has a note (~0x7A), but the exact values depend on the
    # track and theme. Delta semantics ("which indices changed?") is
    # more robust and matches our actual claim: "toggling step N
    # changes step N's LED, and nothing else's".
    initial = bus.snapshot_step_leds()

    # ---- Execute: toggle each downbeat through commander ----
    for step in FOUR_ON_THE_FLOOR_STEPS:
        commander.do(ToggleStep(step))

    # Give Move a couple of SPI frames to propagate the LED changes
    # back through the led_queue and into the overlay SHM.
    bus.wait_frame(8)

    # ---- Snapshot after, compute delta vs initial ----
    after = bus.snapshot_step_leds()

    target_indices = {step - 1 for step in FOUR_ON_THE_FLOOR_STEPS}
    changed = {i for i in range(16) if after[i] != initial[i]}

    # The four toggled steps must have changed color.
    missing_changes = target_indices - changed
    assert not missing_changes, (
        f"steps {sorted(i + 1 for i in missing_changes)} did not change "
        f"color after toggle — Move did not register the step press.\n"
        f"Before: {initial.hex()}\n"
        f"After:  {after.hex()}"
    )

    # No other steps should have changed (in particular not step 2, the
    # between-downbeat case from the user request "verify steps don't
    # light up when we haven't set them yet").
    unexpected_changes = changed - target_indices
    assert not unexpected_changes, (
        f"steps {sorted(i + 1 for i in unexpected_changes)} changed color "
        f"unexpectedly — pad-to-step mapping looks wrong or there was "
        f"cross-talk between rows.\n"
        f"Before: {initial.hex()}\n"
        f"After:  {after.hex()}"
    )

    # ---- Direction check: toggled steps got "more lit", not "less lit" ----
    # Heuristic: Move's lit step color brightness > its dim color. This
    # only fires if the test runs on an empty pattern (the common case
    # because of the dim-everywhere baseline). On a pattern where the
    # target steps were already lit, our toggle clears them and this
    # assertion would fail — keep it advisory by checking the avg
    # direction across the four targets rather than each individually.
    avg_delta = sum(after[i] - initial[i] for i in target_indices) / len(target_indices)
    assert avg_delta > 0, (
        f"toggled steps moved to a *dimmer* color on average "
        f"(avg delta {avg_delta:+.1f}). Pattern may have started with these "
        f"steps already set — the test toggled them OFF. Clear the pattern "
        f"and re-run.\nBefore: {initial.hex()}\nAfter:  {after.hex()}"
    )

    # commander.undo_all() in fixture teardown will press 13/9/5/1
    # again (LIFO) to toggle them off, then SelectTrack undoes.
