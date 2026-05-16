"""Phase 3 commander smoke test — exercise the Command pattern against
the live daemon. SILENT (no pad presses): we only press jog/back and
sample state.

The point isn't to assert deeply on what changes when EnterTrackMenu
runs — without display snapshots we can't really verify Move's menu
state. The point is to verify the infra works end-to-end:

  - STATE command returns valid fields
  - Commander runs a command without exceptions
  - Commander's undo runs at fixture teardown
  - Preconditions fire when state is unexpected
"""

from __future__ import annotations

import pytest

from schwung_bus import BusState, PreconditionError
from schwung_bus.move_commands import EnterTrackMenu, TapButton


def test_state_returns_valid_fields(bus):
    s = bus.state()
    assert isinstance(s, BusState)
    assert 0 <= s.move_ui_mode <= 3, f"unexpected move_ui_mode={s.move_ui_mode}"
    assert 0 <= s.overtake_mode <= 2, f"unexpected overtake_mode={s.overtake_mode}"
    assert s.shift_held in (0, 1)
    assert 0 <= s.selected_slot <= 3
    assert 0 <= s.ui_slot <= 3
    assert s.shim_counter > 0


def test_state_advances_with_frames(bus):
    """Probe twice with a few frames between — counter must advance."""
    s1 = bus.state()
    bus.wait_frame(3)
    s2 = bus.state()
    delta = (s2.shim_counter - s1.shim_counter) & 0xFFFFFFFF
    assert 3 <= delta < 1000, f"shim_counter delta {delta} out of plausible range"


def test_enter_track_menu_round_trip(bus, commander):
    """Run EnterTrackMenu (which taps jog), let commander's teardown undo.

    Asserts only that the command runs without exception. Real verify
    needs display snapshots (Phase 3 later)."""
    s_before = bus.state()
    if not s_before.in_move_native():
        pytest.skip(
            f"Move in schwung overlay (overtake_mode={s_before.overtake_mode}) — "
            "EnterTrackMenu requires Move-native UI"
        )

    commander.do(EnterTrackMenu())
    bus.wait_frame(4)
    s_after = bus.state()
    # We can't strictly assert anything about what changed, but we can
    # assert nothing regressed catastrophically.
    assert s_after.shim_counter > s_before.shim_counter
    # commander.undo_all() runs in fixture teardown


def test_precondition_fires_when_in_overlay(bus, commander, monkeypatch):
    """If we're in schwung overlay, EnterTrackMenu must raise loudly."""
    # We can't easily force ourselves into overlay without producing UI
    # noise. Instead, monkey-patch bus.state() to fake the state.
    fake_state = BusState(
        move_ui_mode=BusState.MOVE_UI_SESSION,
        overtake_mode=BusState.OVERTAKE_MENU,  # in schwung overlay
        shift_held=0,
        selected_slot=0,
        ui_slot=0,
        shim_counter=12345,
        transport_playing=0,
    )
    monkeypatch.setattr(bus, "state", lambda: fake_state)
    with pytest.raises(PreconditionError, match="EnterTrackMenu requires Move-native"):
        commander.do(EnterTrackMenu())
    # Command was NOT pushed, no undo needed
    assert len(commander.stack) == 0


def test_tap_button_undo_is_re_tap(bus, commander):
    """TapButton command's undo re-presses the same button (toggle assumption).

    Use the 'back' button — pressing it twice from a Move-native screen
    should leave us in the same place (toggle into and out of whatever
    back does in the current context). SILENT.
    """
    s_before = bus.state()
    if not s_before.in_move_native():
        pytest.skip("not in Move-native UI")

    commander.do(TapButton("back"))
    bus.wait_frame(4)
    # commander.undo_all() in fixture re-taps; final state should match
    # initial (best-effort; precise verification needs display snapshots)
