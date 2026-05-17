"""Shared helpers for schwung E2E tests.

Plain module (not conftest.py) so it's importable from any test file by
absolute name. conftest.py stays focused on pytest fixtures and
test-runner integration; this file is for logic shared across tests.

Add new helpers here when you find yourself copying a 3+ line block
across two test files. Keep one helper per behavior, document the
contract in its docstring.
"""

from __future__ import annotations

import pytest

from schwung_bus import BusState
from schwung_bus.move_commands import SelectTrack


def enter_note_mode_or_skip(bus, commander, track: int = 1, frames: int = 4) -> None:
    """Select ``track`` via Commander and skip the test if the shim's
    state mirror doesn't end up at NOTE mode.

    The wait length and the skip message are centralised here so every
    test that needs NOTE-mode setup behaves identically. Shared by
    ``test_four_on_the_floor`` and ``test_state_mirror``; call this
    first in any new test that wants to perform step-pad operations.

    Commander records the SelectTrack on the undo stack, so teardown
    un-selects without further action by the caller.
    """
    commander.do(SelectTrack(track))
    bus.wait_frame(frames)
    s = bus.state()
    if s.move_ui_mode != BusState.MOVE_UI_NOTE:
        pytest.skip(
            f"SelectTrack({track}) did not put Move into note-edit mode "
            f"(move_ui_mode={s.move_ui_mode}, want=2). Shim state-mirror "
            "is missing or broken — rebuild + redeploy the shim."
        )
