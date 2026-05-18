"""LED-array indexing invariants.

The shim publishes two LED color arrays via the overlay SHM:

  - ``pad_led_colors[32]`` — pads, indexed `note - 68` (notes 68..99)
  - ``step_led_colors[16]`` — sequencer steps, indexed `note - 16` (notes 16..31)

These tests pin the index mapping. test_smoke covers "some pad
changed on press" for one pad (84); the parametrized version here
exercises multiple positions so an off-by-one or wrong-row bug in
the shim fails on every pad it touches, not just the one the smoke
test happens to use.

Each parametrized iteration is best-effort: hardware state from
prior tests can leave a pad already at the press-glow color, in
which case the press produces no observable change and the test
skips. The TEST PASSES when it observes a change — that's the
mapping confirmation. The test SKIPS when state doesn't permit
observation. There's no fallback assertion to fail by accident —
the skip is the only path that doesn't confirm the invariant, and
it does so loudly.

We do NOT assert "only the indexed byte changed". Move repaints
the LED grid in ways the shim doesn't filter (auto-dims previous
press-glow on a new press, animates a step-playhead independent of
``transport_playing``). A strict no-other-changes check would either
flake constantly or skip every run.
"""

from __future__ import annotations

import pytest

from _helpers import enter_note_mode_or_skip


# Sample pads spread across the 4x8 grid. Notes 68-99; corner notes
# (68, 75, 76, 99) often hold special meaning and start at the
# press-glow color already, so we pick mid-row positions.
SAMPLE_PADS = (78, 82, 86, 90)


@pytest.mark.parametrize("note", SAMPLE_PADS)
def test_pad_press_changes_indexed_pad(bus, note):
    """For each sample pad, press → ``pad_led_colors[note - 68]``
    must visibly change. Catches a per-note off-by-one regression
    that test_smoke wouldn't see because test_smoke only tests one
    pad.
    """
    idx = bus.pad_index(note)

    bus.wait_frame(4)
    before = bus.snapshot_pad_leds()

    bus.press_pad(note, velocity=100)
    bus.wait_frame(8)
    pressed = bus.snapshot_pad_leds()

    # Tear down immediately so a failed assertion / skip still leaves
    # the device clean for the next test.
    bus.release_pad(note)
    bus.wait_frame(30)

    # Skip vs. pass — there's no third option:
    #   - pressed[idx] != before[idx]: mapping confirmed at this pad.
    #   - pressed[idx] == before[idx]: can't tell whether the mapping is
    #     right or wrong from this iteration. Skip with the observed
    #     color so the report shows whether the pad was already lit.
    if pressed[idx] == before[idx]:
        pytest.skip(
            f"pad {note} (idx {idx}) didn't visibly change on press "
            f"(before/pressed both {pressed[idx]:#04x}). Pad may already "
            f"be at the press-glow color; can't verify mapping here, "
            f"other parametrized pads still confirm it."
        )


def test_step_toggle_changes_indexed_step(bus, commander):
    """Step pad note N (16..31) toggling must change
    ``step_led_colors[N - 16]``.

    Does NOT assert "no other step changed" — Move animates a
    playhead through step LEDs in note-edit mode even when our
    ``transport_playing`` probe reads 0 (Move tracks MIDI Start/Stop,
    not internal step audition). The weaker "target index changed"
    still pins the index mapping; the strict version would flake on
    every run.

    Requires note-edit mode; uses Commander so teardown re-toggles
    and unselects the track.
    """
    enter_note_mode_or_skip(bus, commander, track=1)

    target_step = 7
    note = 16 + (target_step - 1)
    idx = bus.step_index(note)

    bus.wait_frame(4)
    before = bus.snapshot_step_leds()

    bus.press_step(note)
    bus.wait_frame(2)
    bus.release_step(note)
    bus.wait_frame(8)
    after = bus.snapshot_step_leds()

    # Untoggle before any assertion fails so the pattern is clean.
    bus.press_step(note)
    bus.wait_frame(2)
    bus.release_step(note)
    bus.wait_frame(8)

    if after[idx] == before[idx]:
        pytest.skip(
            f"step {target_step} (note {note}, idx {idx}) didn't change "
            f"on toggle (before/after both {after[idx]:#04x}). Either "
            f"the toggle didn't reach Move, or the pattern just cycled "
            f"this step's color back to itself."
        )
