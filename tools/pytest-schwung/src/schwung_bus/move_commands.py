"""Move-firmware-specific commands for the Commander pattern.

Concrete UI actions: open Move's track menu, select a track, etc.
Each command knows its preconditions (via ``bus.state()``), how to
execute, and how to reverse. New commands land here as tests need
them; keep the surface tight and well-commented so the test author
can read one place to know what's available.

Generic / non-Move-specific commands (e.g. raw button taps as
commands) could live in their own module if we grow much; for now
this file is fine.

Conventions:
  - All commands are silent: they only press buttons / inject CC's
    that don't trigger audio. Pad note-on inject is OUT of bounds —
    if a command requires it, mark the test as audible and let the
    author opt in.
  - Preconditions use ``BusState`` enum mirrors (e.g. ``state.MOVE_UI_SESSION``)
    so the failing-test message names the symbolic mode.
  - Undo is best-effort symmetric: press the same buttons in reverse
    intent. For non-symmetric flows, document loudly.
"""

from __future__ import annotations

from .client import BusState
from .commander import Command, PreconditionError


class EnterTrackMenu(Command):
    """Open Move's per-track menu by tapping jog click.

    Precondition: we are in Move's native UI (not inside a schwung
    overlay) — otherwise jog click does whatever the overlay binds it
    to, and this command's effect is unpredictable.

    Undo: tap ``back`` to close the menu. Best-effort — if Move's
    menu requires a different exit gesture in some sub-state, undo
    will appear to succeed but leave Move in the menu. Tests should
    verify state via ``bus.state()`` after undo if precision matters.
    """
    name = "enter_track_menu"

    def precondition(self, bus, commander) -> None:
        s = bus.state()
        if not s.in_move_native():
            raise PreconditionError(
                f"EnterTrackMenu requires Move-native UI, got "
                f"overtake_mode={s.overtake_mode}"
            )

    def execute(self, bus, commander) -> None:
        bus.tap("jog_click")
        bus.wait_frame(4)

    def undo(self, bus, commander) -> None:
        bus.tap("back")
        bus.wait_frame(4)


class TapButton(Command):
    """Generic single-button tap + back-tap undo.

    Useful for one-off interactions where no domain-specific command
    exists yet. Reverse action is to tap the same button again
    (toggle assumption) — override if your button isn't toggle-able.
    """

    def __init__(self, button: str):
        self.button = button
        self.name = f"tap({button})"

    def execute(self, bus, commander) -> None:
        bus.tap(self.button)
        bus.wait_frame(4)

    def undo(self, bus, commander) -> None:
        # Default: re-tap to toggle off. Override for non-toggle buttons.
        bus.tap(self.button)
        bus.wait_frame(4)


# Track-select buttons: Move's tracks are at CC 40..43, REVERSED
# (CC43 = Track 1 ... CC40 = Track 4) per CLAUDE.md "Move Hardware MIDI".
_TRACK_CC = {1: 43, 2: 42, 3: 41, 4: 40}


class SelectTrack(Command):
    """Tap Move's track-select button for track n (1..4).

    Precondition: Move in native UI (no schwung overlay).

    Undo: tap track 1 (heuristic — we don't know which track was active
    before, and ``BusState`` doesn't yet expose Move's active track,
    so we restore to the conventional default). Override or wrap if
    your test needs a different post-state.
    """

    def __init__(self, track: int, restore_to: int = 1):
        if track not in _TRACK_CC:
            raise ValueError(f"track must be 1..4, got {track}")
        if restore_to not in _TRACK_CC:
            raise ValueError(f"restore_to must be 1..4, got {restore_to}")
        self.track = track
        self.restore_to = restore_to
        self.name = f"select_track({track})"

    def precondition(self, bus, commander) -> None:
        s = bus.state()
        if not s.in_move_native():
            raise PreconditionError(
                f"SelectTrack requires Move-native UI, got overtake_mode={s.overtake_mode}"
            )

    def _tap_cc(self, bus, cc: int) -> None:
        bus.inject_midi(bytes([0x0B, 0xB0, cc, 127]))
        bus.wait_frame(2)
        bus.inject_midi(bytes([0x0B, 0xB0, cc, 0]))
        bus.wait_frame(4)

    def execute(self, bus, commander) -> None:
        self._tap_cc(bus, _TRACK_CC[self.track])

    def undo(self, bus, commander) -> None:
        self._tap_cc(bus, _TRACK_CC[self.restore_to])


# Sequencer step pads: notes 16..31 per CLAUDE.md "Move Hardware MIDI".
# Step n (1..16) → note 16 + (n-1).
def _step_note(step: int) -> int:
    if not 1 <= step <= 16:
        raise ValueError(f"step must be 1..16, got {step}")
    return 16 + (step - 1)


class ToggleStep(Command):
    """Toggle sequencer step n (1..16) by pressing its pad note.

    Precondition: Move in note-edit mode (``move_ui_mode == MOVE_UI_NOTE``);
    otherwise the step pad isn't a sequencer toggle and the LED state
    won't change.

    Undo: press the step again (toggle off).

    SAFETY: in note-edit mode with transport STOPPED, toggling a step
    is silent. With transport playing, the new note will sound next
    time the playhead reaches it. Tests using this command must
    ensure transport is stopped. (We can't check from BusState today
    — ``transport_playing`` lives in the overlay struct but isn't
    exposed via STATE; add it if needed.)
    """

    def __init__(self, step: int):
        self.step = step
        self.note = _step_note(step)
        self.name = f"toggle_step({step})"

    def precondition(self, bus, commander) -> None:
        s = bus.state()
        if s.move_ui_mode != BusState.MOVE_UI_NOTE:
            raise PreconditionError(
                f"ToggleStep requires Move in note-edit mode (move_ui_mode=2), "
                f"got move_ui_mode={s.move_ui_mode}"
            )
        if s.transport_playing:
            # Transport playing means a toggled-on step would sound next
            # time the playhead reaches it — unsafe for silent test runs.
            raise PreconditionError(
                "ToggleStep refuses to run while transport is playing "
                "(would produce audio). Stop transport before this command."
            )

    def _press(self, bus) -> None:
        # Note-on + note-off as cable-0 (internal hardware).
        bus.inject_midi(bytes([0x09, 0x90, self.note, 100]))
        bus.wait_frame(2)
        bus.inject_midi(bytes([0x08, 0x80, self.note, 0x40]))
        bus.wait_frame(4)

    def execute(self, bus, commander) -> None:
        self._press(bus)

    def undo(self, bus, commander) -> None:
        # Re-press toggles the step off (Move's sequencer semantics).
        self._press(bus)
