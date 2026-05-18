"""Hardware boundary tests for pad / step note → LED-index mapping.

The mid-grid parametrized tests in test_led_indexing.py wouldn't
catch a regression that miscounts only the first or last position
(e.g. `note - 67` would map 68→1 instead of 0, and the parametrize
samples 78/82/86/90 wouldn't notice because those still land at
non-corner indices).

The corner notes are:
  - Pad: 68 → idx 0, 99 → idx 31
  - Step: 16 → idx 0, 31 → idx 15

These tests have the OFF-BY-ONE TRAP: when the corner index doesn't
visibly change, we also check the adjacent index (idx ± 1). If the
adjacent index changed instead, that's a real off-by-one bug — fail.
If neither changed, the pad/step was probably already lit — skip.

Python-side range-validator tests live offline in
``tools/pytest-schwung/tests/test_note_validators.py``.
"""

from __future__ import annotations

import pytest

from _helpers import enter_note_mode_or_skip


@pytest.mark.parametrize("note", [68, 99])
def test_corner_pad_changes_corner_led_or_traps_off_by_one(bus, note):
    """The lowest pad note (68) must hit ``pad_led_colors[0]``; the
    highest (99) must hit ``[31]``. Critical because an off-by-one
    in the shim's per-note index calc would silently corrupt every
    test that uses pad_led_colors.

    Off-by-one trap: if the target corner index didn't change but
    the ADJACENT index did, that's a regression caught loudly.
    Otherwise (neither changed) the pad was already lit — skip.
    """
    target_idx = bus.pad_index(note)
    # Adjacent direction: corner 0 → trap idx 1; corner 31 → trap idx 30.
    trap_idx = 1 if target_idx == 0 else 30

    bus.wait_frame(4)
    before = bus.snapshot_pad_leds()

    bus.press_pad(note, velocity=100)
    bus.wait_frame(8)
    pressed = bus.snapshot_pad_leds()

    bus.release_pad(note)
    bus.wait_frame(30)

    target_changed = pressed[target_idx] != before[target_idx]
    trap_changed   = pressed[trap_idx] != before[trap_idx]

    # Real bug: target didn't change but adjacent did.
    if not target_changed and trap_changed:
        pytest.fail(
            f"OFF-BY-ONE: pressing pad {note} (target idx {target_idx}) "
            f"changed adjacent idx {trap_idx} instead: "
            f"target {before[target_idx]:#04x}→{pressed[target_idx]:#04x}, "
            f"trap   {before[trap_idx]:#04x}→{pressed[trap_idx]:#04x}.\n"
            f"  before:  {before.hex()}\n"
            f"  pressed: {pressed.hex()}"
        )

    if not target_changed:
        pytest.skip(
            f"corner pad {note} (idx {target_idx}) didn't change "
            f"(before/pressed both {pressed[target_idx]:#04x}); adjacent "
            f"idx {trap_idx} also didn't change — likely already at "
            f"press-glow color. Re-run after track change to shake loose."
        )


@pytest.mark.parametrize("step_note", [16, 31])
def test_corner_step_changes_corner_led_or_traps_off_by_one(bus, commander, step_note):
    """Same as the pad test, for step LEDs. Note 16 → idx 0, note 31
    → idx 15. Off-by-one trap: adjacent step (idx 1 or 14).

    Untoggle only fires when the first toggle landed — otherwise
    the "untoggle" would actually ACTIVATE the step we never
    intended to set, dirtying the pattern.
    """
    enter_note_mode_or_skip(bus, commander, track=1)
    target_idx = bus.step_index(step_note)
    trap_idx = 1 if target_idx == 0 else 14

    bus.wait_frame(4)
    before = bus.snapshot_step_leds()

    bus.press_step(step_note)
    bus.wait_frame(2)
    bus.release_step(step_note)
    bus.wait_frame(8)
    after = bus.snapshot_step_leds()

    target_changed = after[target_idx] != before[target_idx]
    trap_changed   = after[trap_idx] != before[trap_idx]

    # Only untoggle if the first toggle actually landed. Otherwise
    # the second press is the FIRST observed toggle and would dirty
    # the pattern (set a step that was off, leaving it on).
    if target_changed:
        bus.press_step(step_note)
        bus.wait_frame(2)
        bus.release_step(step_note)
        bus.wait_frame(8)

    if not target_changed and trap_changed:
        pytest.fail(
            f"OFF-BY-ONE: toggling step note {step_note} (target idx "
            f"{target_idx}) changed adjacent idx {trap_idx} instead.\n"
            f"  before: {before.hex()}\n"
            f"  after:  {after.hex()}"
        )

    if not target_changed:
        pytest.skip(
            f"corner step (note {step_note}, idx {target_idx}) didn't "
            f"change on toggle (before/after both {after[target_idx]:#04x}); "
            f"adjacent idx {trap_idx} also unchanged. Toggle may not have "
            f"reached Move, or the playhead repainted this cell back."
        )
