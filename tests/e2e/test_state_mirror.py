"""Shim state-mirror regression tests.

These exercise the post-merge scan that copies a few fields of the
shim's internal state into ``shadow_control_t`` for injected MIDI
events (track buttons today; expand as needs grow).

The mirror is the contract that lets Commander preconditions (and any
``bus.state()``-based assertion) trust that injection has happened.
Without it, every test that wants to verify "tap X → state mirror
says Y" would have to fall back to LED scraping.

What's mirrored from inject events (in ``src/schwung_shim.c``, after
``shadow_drain_midi_inject()``):
  - ``move_ui_mode`` → 2 (NOTE) on any track-CC press
  - ``selected_slot``, ``ui_slot`` → 43 - CC# (CC43=T1 ... CC40=T4)

What's NOT mirrored yet (hardware-only):
  - ``shift_held`` (CC 49) — needs the shim's ``shift_armed`` debounce
    logic to be honored, which is non-trivial. Tests that depend on
    shift will need to live with hardware-only verification or a
    dedicated daemon command in the future.
"""

from __future__ import annotations

import pytest

from schwung_bus import BusState
from schwung_bus.move_commands import SelectTrack

from _helpers import enter_note_mode_or_skip


def test_each_track_selects_correct_slot(bus, commander):
    """Tap each track button (1..4) via SelectTrack and confirm the
    shim's ``selected_slot`` mirror lands on the expected value.

    Tracks are CC-reversed on Move: CC43=Track1 ... CC40=Track4. The
    canonical mapping `slot = 43 - CC#` lives in two places (shim's
    post-merge scan and the existing hardware-buffer scan) — if they
    drift, this test catches it on the next run.
    """
    initial_slot = bus.state().selected_slot
    seen_slots: list[int] = []
    expected = [0, 1, 2, 3]  # tracks 1..4

    for track in (1, 2, 3, 4):
        commander.do(SelectTrack(track, restore_to=1))
        bus.wait_frame(4)
        s = bus.state()
        seen_slots.append(s.selected_slot)
        assert s.ui_slot == s.selected_slot, (
            f"ui_slot ({s.ui_slot}) drifted from selected_slot "
            f"({s.selected_slot}) after tapping track {track}"
        )

    assert seen_slots == expected, (
        f"track-to-slot mapping wrong: tapped tracks {[1,2,3,4]}, "
        f"got slots {seen_slots}, expected {expected}. "
        f"(initial slot was {initial_slot})"
    )


def test_track_tap_marks_move_in_note_mode(bus, commander):
    """A track tap puts Move into NOTE view. Verify the shim mirror
    records that — otherwise Commander preconditions that gate on
    NOTE mode (e.g. ToggleStep) skip even when Move is actually there.

    This is the regression the four-on-floor test originally hit. Uses
    the shared ``enter_note_mode_or_skip`` helper from conftest so the
    skip message and the wait length stay consistent with every other
    test that needs NOTE-mode setup.
    """
    enter_note_mode_or_skip(bus, commander, track=1)
    s = bus.state()
    assert s.move_ui_mode == BusState.MOVE_UI_NOTE, (
        f"helper said NOTE mode reached but state still reads "
        f"{s.move_ui_mode} — helper is lying or the mirror is racy."
    )


def test_track_switch_changes_step_led_pattern(bus, commander):
    """Different tracks usually carry different step patterns. Tapping
    track A and snapshotting step LEDs, then track B and snapshotting,
    should produce two snapshots that aren't byte-identical — unless
    the patterns happen to match coincidentally, in which case we skip.

    Catches the regression where Move's display fails to refresh step
    LEDs after a track switch (shim mishandles led_queue replay,
    overlay SHM stale, etc.).
    """
    commander.do(SelectTrack(1))
    bus.wait_frame(8)
    pattern_t1 = bus.snapshot_step_leds()

    commander.do(SelectTrack(2, restore_to=1))
    bus.wait_frame(8)
    pattern_t2 = bus.snapshot_step_leds()

    if pattern_t1 == pattern_t2:
        pytest.skip(
            f"track 1 and track 2 have identical step patterns "
            f"({pattern_t1.hex()}) — can't distinguish a real refresh "
            "from a stale snapshot. Set different notes on the two tracks."
        )

    # Sanity: returning to track 1 should bring back pattern_t1.
    commander.do(SelectTrack(1, restore_to=1))
    bus.wait_frame(8)
    pattern_t1_again = bus.snapshot_step_leds()

    assert pattern_t1_again == pattern_t1, (
        f"step LEDs didn't restore on switching back to track 1.\n"
        f"  first snapshot:  {pattern_t1.hex()}\n"
        f"  second snapshot: {pattern_t1_again.hex()}\n"
        f"This suggests Move re-painted from the wrong pattern source "
        f"(or led_queue is dropping events)."
    )
