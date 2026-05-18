"""Schwung shim invariant tests.

These tests pin down behaviors the shim *claims* to have, regardless
of what loaded modules do. They're meant to fail loudly if a future
shim refactor accidentally widens (or narrows) any of these filters.

Scope: only invariants exercisable via the test-bus today. Things
that need shift-held state, real hardware jack toggles, or MIDI_OUT-
driven transport state are out of scope — see the README pitfalls
section and `flagist0/schwung#2` for the running list of what's not
yet observable.

Conventions:
  - Each test starts from a known slot focus (track 2 = slot 1) via
    Commander so teardown reverts cleanly.
  - Assertions are framed as "X did not change slot/state" rather
    than absolute values — the starting state is set by the test.
  - Use raw `bus.inject_midi(...)` only when testing the inject path
    itself; for behavioral setup, prefer Commander.
"""

from __future__ import annotations

import pytest

from schwung_bus.move_commands import SelectTrack


# ---------------------------------------------------------------------------
# Track-CC mirror — boundary / cable / press-vs-release filters
# ---------------------------------------------------------------------------

def test_cc_just_below_track_range_does_not_change_slot(bus, commander):
    """Track CCs live in [40..43]. CC 39 (Mute control on some
    layouts) must not be misread as a track press by the state
    mirror. Catches an off-by-one in the `d1 < 40` lower bound.
    """
    commander.do(SelectTrack(2, restore_to=1))
    bus.wait_frame(4)
    assert bus.state().selected_slot == 1, "test setup failed (SelectTrack)"

    bus.inject_midi(bytes([0x0B, 0xB0, 39, 127]))
    bus.wait_frame(4)
    bus.inject_midi(bytes([0x0B, 0xB0, 39, 0]))
    bus.wait_frame(4)

    s = bus.state()
    assert s.selected_slot == 1, (
        f"CC 39 (just below track range 40-43) changed selected_slot "
        f"from 1 to {s.selected_slot} — lower-bound off-by-one in the "
        f"shim's state-mirror filter."
    )


def test_cc_just_above_track_range_does_not_change_slot(bus, commander):
    """Same as above for CC 44 — catches upper-bound off-by-one
    (`d1 > 43`).
    """
    commander.do(SelectTrack(2, restore_to=1))
    bus.wait_frame(4)
    assert bus.state().selected_slot == 1, "test setup failed (SelectTrack)"

    bus.inject_midi(bytes([0x0B, 0xB0, 44, 127]))
    bus.wait_frame(4)
    bus.inject_midi(bytes([0x0B, 0xB0, 44, 0]))
    bus.wait_frame(4)

    s = bus.state()
    assert s.selected_slot == 1, (
        f"CC 44 (just above track range 40-43) changed selected_slot "
        f"from 1 to {s.selected_slot} — upper-bound off-by-one in the "
        f"shim's state-mirror filter."
    )


def test_track_cc_release_only_does_not_change_slot(bus, commander):
    """The state mirror checks `d2 == 0 → skip`: only presses
    (d2 > 0) should advance the slot. A release-only event (e.g. an
    application sending CC 41 = 0 without the preceding press)
    must be a no-op.
    """
    commander.do(SelectTrack(2, restore_to=1))
    bus.wait_frame(4)
    assert bus.state().selected_slot == 1, "test setup failed"

    # Release-only for CC 41 (Track 3, slot 2). If the press check is
    # missing, slot would jump to 2.
    bus.inject_midi(bytes([0x0B, 0xB0, 41, 0]))
    bus.wait_frame(4)

    s = bus.state()
    assert s.selected_slot == 1, (
        f"track-CC release with d2=0 changed selected_slot from 1 to "
        f"{s.selected_slot} — the mirror's press-vs-release filter is broken."
    )


def test_cable_2_track_cc_does_not_change_slot(bus, commander):
    """The mirror filters to cable=0 (Move internal). The same CC on
    cable=2 (external USB MIDI in) means "an external controller sent
    a CC 40 message" — that's a parameter the host might route through
    chain MIDI FX, NOT a track-button press. Misreading it as a press
    would silently steal slot focus from whatever module was using it.

    Catches a regression where the cable filter is widened from `== 0`
    to "anything" or where the post-merge scan accidentally reads
    cable-2 events.
    """
    commander.do(SelectTrack(2, restore_to=1))
    bus.wait_frame(4)
    assert bus.state().selected_slot == 1, "test setup failed"

    # High nibble of byte 0 = cable; CIN 0xB = CC.
    bus.inject_midi(bytes([0x2B, 0xB0, 40, 127]))
    bus.wait_frame(4)
    bus.inject_midi(bytes([0x2B, 0xB0, 40, 0]))
    bus.wait_frame(4)

    s = bus.state()
    assert s.selected_slot == 1, (
        f"cable=2 CC 40 changed selected_slot from 1 to {s.selected_slot} "
        f"— the cable filter in the shim's state-mirror is broken."
    )


def test_all_notes_off_does_not_change_slot(bus, commander):
    """CC 123 (All Notes Off, channel-mode message) shares the CC
    status byte with track CCs but lives outside [40..43]. This
    test exists because conftest's autouse teardown emits CC 123
    after every test; if the mirror's range filter is broken, the
    teardown itself would silently corrupt slot state between tests.
    """
    commander.do(SelectTrack(2, restore_to=1))
    bus.wait_frame(4)
    assert bus.state().selected_slot == 1, "test setup failed"

    bus.inject_midi(bytes([0x0B, 0xB0, 123, 127]))
    bus.wait_frame(4)
    bus.inject_midi(bytes([0x0B, 0xB0, 123, 0]))
    bus.wait_frame(4)

    s = bus.state()
    assert s.selected_slot == 1, (
        f"CC 123 (All Notes Off) changed selected_slot from 1 to "
        f"{s.selected_slot} — the mirror's range filter doesn't gate "
        f"out non-track CCs. Per-test teardown would corrupt slot focus."
    )


# ---------------------------------------------------------------------------
# Track-CC mirror — idempotence and ordering
# ---------------------------------------------------------------------------

def test_repeated_same_track_press_is_idempotent(bus, commander):
    """Pressing the same track button twice should not change the
    slot (it's already selected). Catches a regression where the
    mirror toggles or rotates on each press.
    """
    commander.do(SelectTrack(3, restore_to=1))
    bus.wait_frame(4)
    s_first = bus.state()
    assert s_first.selected_slot == 2, "test setup failed"
    counter_first = s_first.shim_counter

    # Tap track 3 (CC 41) again — should be a no-op
    bus.inject_midi(bytes([0x0B, 0xB0, 41, 127]))
    bus.wait_frame(2)
    bus.inject_midi(bytes([0x0B, 0xB0, 41, 0]))
    bus.wait_frame(4)

    s_second = bus.state()
    assert s_second.selected_slot == 2, (
        f"re-tapping the already-selected track shifted slot to "
        f"{s_second.selected_slot} — mirror isn't idempotent."
    )
    # Sanity: shim is still advancing the counter (not stuck).
    assert s_second.shim_counter > counter_first


def test_rapid_track_cycle_lands_on_last_press(bus, commander):
    """Mash tracks 1→2→3→4 with minimal spacing — final slot should
    reflect the LAST press. Catches a regression where the mirror
    debounces or aliases rapid events.

    NB: each `tap` waits 4 frames before the next press by default
    (see SelectTrack._tap_cc). That's ~12ms — fast enough to stress
    the path but not instantaneous.
    """
    expected_final_slot = 3  # track 4
    for track in (1, 2, 3, 4):
        commander.do(SelectTrack(track, restore_to=1))

    bus.wait_frame(8)  # extra settle
    s = bus.state()
    assert s.selected_slot == expected_final_slot, (
        f"after cycling tracks 1→2→3→4 the mirror landed on slot "
        f"{s.selected_slot}, expected {expected_final_slot}. "
        f"Mirror may be aliasing or running on stale events."
    )


# ---------------------------------------------------------------------------
# Restart-move invariants — what survives vs. what resets
# ---------------------------------------------------------------------------

def test_restart_move_resets_overtake_mode(bus, fresh_move):
    """Documented contract from commit 517fa4f0: shim's ``shm_init``
    explicitly resets ``overtake_mode`` to 0 on every load. The SHM
    file (``/dev/shm/schwung-control``) survives restart-move, so
    without this reset the new shim inherits the previous session's
    overtake state — and Move's surface stays dark because the
    firmware thinks an overtake module owns LED output.

    This test keeps that fix in place.
    """
    s = bus.state()
    assert s.overtake_mode == 0, (
        f"after restart-move, overtake_mode is {s.overtake_mode}, "
        f"expected 0. The shim's shm_init forgot to reset overlay state — "
        f"see commit 517fa4f0 for the surface-stays-dark regression."
    )


def test_restart_move_resets_selected_slot(bus, commander, fresh_move):
    """Same documented contract as above: ``selected_slot`` is one of
    the four fields explicitly zeroed in ``shm_init``. The test sets
    slot=2 before the restart so a stuck-at-previous-value bug shows
    up clearly.

    NB: ``fresh_move`` runs in fixture setup; the SelectTrack here
    runs AFTER the restart, then the restart-move flag write happens
    again? No — ``fresh_move`` is a function-scoped fixture that
    yields once. So the order is: SelectTrack from prior test (sets
    slot non-zero), restart-move (should reset), assert slot==0.

    The autouse ``_bus_state_cleanup`` only emits All-Notes-Off, not
    a SelectTrack, so it doesn't undo the restart-move reset.
    """
    s = bus.state()
    assert s.selected_slot == 0, (
        f"after restart-move, selected_slot is {s.selected_slot}, "
        f"expected 0. Lost contract from commit 517fa4f0."
    )


def test_restart_move_does_not_reset_move_ui_mode(bus, fresh_move):
    """Documents the CURRENT shim behavior: ``move_ui_mode`` persists
    across restart-move. The shim's ``shm_init`` reset list (per
    517fa4f0) is exactly ``overtake_mode / suspend_overtake /
    selected_slot / skip_led_clear`` — ``move_ui_mode`` is not in it.

    This is probably fine — Move firmware re-emits a track press on
    user input and the mirror corrects itself. But it's a gap worth
    knowing about; if a test ever assumes a clean ``move_ui_mode``
    after restart, that test will see stale state.

    Marked as the contract, not a regression: change the assertion if
    the shim is updated to reset ``move_ui_mode`` too.
    """
    # Use SelectTrack BEFORE the restart so the mirror is at 2.
    # But fresh_move ran in setup, so the prior state is whatever the
    # last test left. We can only check that *if* the prior state had
    # move_ui_mode=2, restart didn't reset it.
    s = bus.state()
    # No assert on direction — just record the current value with a
    # message so a future regression on either side is visible.
    if s.move_ui_mode == 0:
        pytest.skip(
            "prior test left move_ui_mode=0 already, can't observe "
            "the persistence contract from this side. Run after a "
            "test that taps a track button."
        )
    assert s.move_ui_mode == 2, (
        f"move_ui_mode after restart-move is {s.move_ui_mode} — "
        f"expected 2 (persisted from prior test). If the shim now "
        f"resets it, update this test and the documentation."
    )


def test_restart_move_does_not_reset_display_mode_TODO(bus, fresh_move):
    """**Known gap, intentional test marker.**

    ``display_mode`` (1 = shadow UI displayed) is not in the
    517fa4f0 reset list. After restart-move, the shadow_ui process
    is killed and relaunched, but the SHM bit saying "shadow UI is
    displayed" can survive at 1 from the previous session.

    Whether the relaunched shadow_ui interprets that correctly (and
    re-paints itself into the displayed state) is the question. If
    it does, leaving the bit at 1 is fine. If it doesn't, the
    surface lies about what's on screen.

    This test SKIPs today with a TODO link so the gap is on the
    record. Investigate and either widen the reset list or document
    the behavior precisely, then convert the skip to an assertion
    matching the chosen contract.
    """
    s = bus.state()
    pytest.skip(
        f"TODO investigate: display_mode after restart-move is "
        f"{s.display_mode}. The 517fa4f0 reset list excludes it; "
        f"unclear whether the relaunched shadow_ui correctly recovers "
        f"from a persisted display_mode=1. File a follow-up if this "
        f"causes user-visible surface inconsistency."
    )
