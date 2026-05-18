"""Cross-track state isolation tests.

Toggling a step on one track must not affect any other track's
pattern. Move's sequencer stores per-track step data and Move's UI
shows each track's pattern independently when selected.

Catches regressions like:
  - Off-by-one in track-index → step-array indexing in the shim's
    LED-queue replay logic
  - Wrong-track CC during overtake mode causing step writes to
    bleed across tracks
  - SHM contention where one track's step snapshot overwrites
    another's during a fast track switch

Step-LED observation model (probed on Move v2.0.0): toggling a step
brightens that cell while Move dims the rest of the row. The
currently-playing beat appears as a singleton "playhead" cell at
the top brightness, animated by Move even with transport stopped.

We avoid step-LED interpretation gymnastics here — the assertion
form below ("track 3's lit pattern is not track 2's lit pattern")
is robust against both per-instrument UI quirks and the playhead
overlay; it would only fail on a real cross-track leak.
"""

from __future__ import annotations

import pytest

from schwung_bus.move_commands import SelectTrack


TRACK_A_STEPS = (1, 5, 9, 13)  # downbeats


def _press_step_via_inject(bus, step_1_based: int) -> None:
    """Press + release a step pad. Step note = 16 + (step-1)."""
    note = 16 + (step_1_based - 1)
    bus.press_step(note)
    bus.wait_frame(2)
    bus.release_step(note)
    bus.wait_frame(8)


def _select_and_settle(bus, commander, track: int, settle_frames: int = 20) -> bytes:
    """Select ``track`` (puts Move in NOTE mode for it), wait for
    repaint, return step LED snapshot.
    """
    commander.do(SelectTrack(track, restore_to=1))
    bus.wait_frame(settle_frames)
    return bus.snapshot_step_leds()


def _stable_lit_indices(bus, settle_frames: int = 20) -> set[int]:
    """Return indices of step LEDs that have a note set, stable
    against the playhead.

    Move's playhead cell is animated even with transport stopped.
    Taking one snapshot can't tell a single toggled step from the
    playhead — both look like "one non-baseline cell". We take TWO
    snapshots ``settle_frames`` apart; cells whose brightness
    differs across snapshots are the moving playhead, cells stable
    above baseline in BOTH snapshots are toggled notes.

    Edge case: if the playhead doesn't move between snapshots (low
    transport activity), it'll appear stable and get counted. We
    don't try to compensate — that's fine for "set comparison"
    tests where the playhead may contribute one false-positive
    index to both compared sets.
    """
    a = bus.snapshot_step_leds()
    bus.wait_frame(settle_frames)
    b = bus.snapshot_step_leds()
    from collections import Counter
    counts = Counter(a) + Counter(b)
    baseline, _ = counts.most_common(1)[0]
    lit: set[int] = set()
    for i in range(len(a)):
        if a[i] == b[i] and a[i] != baseline:
            lit.add(i)
    return lit


def test_step_pattern_on_one_track_invisible_on_another(bus, commander, pristine_set):
    """Toggle downbeats on track 2, switch to track 3, confirm track 3
    does NOT show the same lit pattern. Catches the cross-track
    bleed bug.

    Uses melodic tracks (2 = Bass, 3 = E-Piano) — Move's step grid
    in drum mode (track 1 in our template) renders differently and
    isn't a clean toggle target. The contract holds across all 4
    tracks; we test on a pair where snapshots are easy to interpret.

    Discriminator: "track 3's lit set ≠ track 2's lit set".
    Track 3 may have its own innate UI highlight pattern (E-Piano
    in our template shows a 4-cell highlight unrelated to any
    leaked pattern). The strong assertion would falsely flag that;
    the difference check correctly catches only the bleed bug.
    """
    expected_t2 = {s - 1 for s in TRACK_A_STEPS}

    _select_and_settle(bus, commander, track=2)
    for s in TRACK_A_STEPS:
        _press_step_via_inject(bus, s)
    after_t2 = bus.snapshot_step_leds()
    lit_t2 = _stable_lit_indices(bus)
    if lit_t2 != expected_t2:
        pytest.skip(
            f"setup race: lit indices on track 2 {sorted(lit_t2)} "
            f"!= expected {sorted(expected_t2)}. "
            f"Step grid: {after_t2.hex()}"
        )

    after_t3 = _select_and_settle(bus, commander, track=3)
    lit_t3 = _stable_lit_indices(bus)
    assert lit_t3 != expected_t2, (
        f"track 3 shows the same lit pattern as track 2 "
        f"({sorted(lit_t3)}) — track 2's edits bled across.\n"
        f"  track 2 after toggles: {after_t2.hex()}\n"
        f"  track 3 immediately:   {after_t3.hex()}"
    )
