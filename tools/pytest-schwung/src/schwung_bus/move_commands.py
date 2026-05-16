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
