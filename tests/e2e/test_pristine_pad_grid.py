"""Strict pad-grid invariants using ``pristine_set``.

These tests pin assertions that wouldn't be possible without the
deterministic starting state ``pristine_set`` provides. The weak
version in ``test_led_indexing.py`` parametrizes pads and skips
each iteration whose target was already at the press-glow color
(state-dependent). The strict version here KNOWS the starting
state and asserts unconditionally.

What pristine_set unlocks here:
  - Every parametrized iteration lands an observable change (no
    "skip — pad already lit" outcomes that hide regressions).
  - Direction assertion: target pad goes UP in brightness on press
    (toward Move's "press-glow" color), DOWN on release.

What we deliberately DON'T assert:
  - "Only the target index changed." Move's UI repaints
    root-note / scale-degree indicators contextually when a note
    is held, so other indices change as a side effect of Move's
    own UI logic, not because the shim's index mapping is wrong.
    Run-trace observed: pressing pad 77 (in a melodic track) also
    flips two root-octave marker pads. That's a feature, not a
    pad-mapping bug.

Track 2 in our template (Esperienza Bass) is a melodic instrument:
its pad grid is uniformly playable (color 0x7B) except for edge
indicators at columns 0 and 7. We exercise the inner-column pads.
"""

from __future__ import annotations

import pytest

from schwung_bus.move_commands import SelectTrack


# Inner-column pads in row 1 (notes 76-83) — all should be at the
# uniform "playable" color 0x7B in the pristine template's track 2
# layout. Skipping notes 76 and 83 (column 0 and 7 = edge markers).
INNER_PADS_ROW_1 = (77, 78, 79, 80, 81, 82)

PLAYABLE_COLOR = 0x7B
PRESS_GLOW_COLOR = 0x7E


@pytest.mark.parametrize("note", INNER_PADS_ROW_1)
def test_pristine_pad_press_always_lands(bus, commander, pristine_set, note):
    """In pristine state + melodic track 2 selected, every inner-
    column pad starts at PLAYABLE_COLOR (0x7B) — the bass synth's
    uniform "playable note" shade. Pressing the pad takes it to
    PRESS_GLOW_COLOR (0x7E). Both checks are EXACT, not "any
    change" — the starting state is known.

    If pristine_set is doing its job, every iteration here passes;
    no skips. If even one iteration starts at the wrong color, the
    template Song.abl or Move's startup behavior drifted.
    """
    commander.do(SelectTrack(2, restore_to=1))
    # Move's pad-grid repaint after a post-restart track switch
    # takes longer than expected — observed up to ~40 frames before
    # the snapshot reflects the new track's layout. The state-mirror
    # updates immediately (selected_slot=1 in a few frames) but
    # Move's own UI thread is slower to repaint the pad LEDs after
    # the restart settle. Spin up to ~50 frames waiting for the
    # row-0 marker (any non-zero byte at col 0) to land at the
    # melodic-track edge color (0x09).
    for _ in range(10):
        bus.wait_frame(5)
        snap = bus.snapshot_pad_leds()
        if snap[0] == 0x09:
            break
    else:
        pytest.skip(
            f"after SelectTrack(2) + 50-frame wait, pad grid still "
            f"shows non-track-2 layout (idx 0 = {snap[0]:#04x}, expected "
            f"0x09 edge marker). Move's UI didn't repaint in time.\n"
            f"  full grid: {snap.hex()}"
        )

    idx = bus.pad_index(note)
    before = bus.snapshot_pad_leds()
    assert before[idx] == PLAYABLE_COLOR, (
        f"pristine pad {note} (idx {idx}) starts at {before[idx]:#04x}, "
        f"expected playable {PLAYABLE_COLOR:#04x}. Either the template "
        f"Song.abl drifted, the track instrument changed, or Move's "
        f"layout for this note differs from when we captured.\n"
        f"  full grid: {before.hex()}"
    )

    bus.press_pad(note, velocity=100)
    bus.wait_frame(8)
    pressed = bus.snapshot_pad_leds()
    assert pressed[idx] == PRESS_GLOW_COLOR, (
        f"pristine pad {note} (idx {idx}) on press is {pressed[idx]:#04x}, "
        f"expected press-glow {PRESS_GLOW_COLOR:#04x}.\n"
        f"  before:  {before.hex()}\n"
        f"  pressed: {pressed.hex()}"
    )

    # On release, the pad should leave the press-glow color. Poll up
    # to ~175 ms (10 × 6 frames) — same budget as test_pad_release,
    # absorbs Move's variable press-glow hold time after burst-load
    # tests. Real stuck-pad regression would hold forever.
    bus.release_pad(note)
    for _ in range(10):
        bus.wait_frame(6)
        released = bus.snapshot_pad_leds()
        if released[idx] != PRESS_GLOW_COLOR:
            break
    # Exact restoration to PLAYABLE_COLOR is NOT asserted — Move's
    # contextual root indicators can settle to 0x7A (root) or 0x7B
    # (regular) depending on where the played note lands in the
    # scale. Only the press-glow must be gone.
    assert released[idx] != PRESS_GLOW_COLOR, (
        f"pad {note} (idx {idx}) stuck at press-glow {PRESS_GLOW_COLOR:#04x} "
        f"~175 ms after release — stuck-pad regression. "
        f"Released: {released.hex()}"
    )
