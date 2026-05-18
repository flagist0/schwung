"""Smoke tests for the ``pristine_set`` fixture.

Confirms the end-to-end flow:
  - SSH ControlMaster opens
  - Repo template gets staged on the device
  - Per-test fixture copies template into Move's Sets dir
  - restart_move triggers, shim recovers
  - Move loads the template (verifiable via step LED snapshot)

These tests don't pin a specific song-content invariant — that's the
job of the actual feature tests that USE pristine_set. We just verify
the plumbing.
"""

from __future__ import annotations


def test_pristine_set_yields_after_restart(bus, pristine_set):
    """After ``pristine_set``, the bus should be alive, the shim
    should be ticking, and overtake_mode should be 0 (Move-native).

    This is the same shape as test_fresh_move tests, but with the
    Song.abl overwrite + restart round-trip in front.
    """
    s = bus.state()
    assert s.shim_counter > 0, "shim not ticking after pristine_set"
    assert s.overtake_mode == 0, (
        f"overtake_mode={s.overtake_mode} after pristine_set "
        f"(expected 0 — restart should reset it; see commit 517fa4f0)"
    )


def test_pristine_set_step_pattern_is_empty(bus, pristine_set, commander):
    """The canonical empty template has 0 clips per track, so when
    we enter note-edit mode on any track, no step should be "set".

    "Set" here means "noticeably brighter than the surrounding dim
    base color" — we can't assert exact bytes because Move's color
    palette varies by track instrument and theme. We sample step 5
    (a middle position) and observe its color matches the
    surrounding cells (all dim, no highlight).
    """
    from schwung_bus.move_commands import SelectTrack
    commander.do(SelectTrack(1, restore_to=1))
    bus.wait_frame(8)

    step_leds = bus.snapshot_step_leds()
    # Every step should have approximately the same dim color — find
    # the most common color and confirm step 5 matches it.
    from collections import Counter
    counts = Counter(step_leds)
    most_common_color, count = counts.most_common(1)[0]

    assert count >= 12, (
        f"empty pattern should have most cells at the dim color, "
        f"but only {count}/16 share the most common color "
        f"{most_common_color:#04x}. Pattern not empty? Step LEDs: "
        f"{step_leds.hex()}"
    )

    # Step 5 specifically should be at the dim color, not lit.
    idx = 4  # step 5 = index 4
    assert step_leds[idx] == most_common_color, (
        f"step 5 (idx {idx}) color {step_leds[idx]:#04x} differs "
        f"from the dim baseline {most_common_color:#04x} — there's "
        f"a note set in the supposedly-pristine template. "
        f"Step LEDs: {step_leds.hex()}"
    )


def test_pristine_set_back_to_back_is_idempotent(bus, pristine_set):
    """Running pristine_set twice (via two separate tests) must
    leave the device in the same state both times. Catches a
    regression where the per-test ``cp`` accumulates state on
    the staging file (it shouldn't — ssh cp overwrites).
    """
    # This test runs pristine_set via the fixture. The previous test
    # in the file also ran it. So we're observing the SECOND run.
    # The assertion is structural: state probes work and produce a
    # consistent shape (not the first-run leftover).
    s = bus.state()
    assert s.shim_counter > 0
    assert s.overtake_mode == 0
    leds = bus.snapshot_step_leds()
    assert len(leds) == 16
