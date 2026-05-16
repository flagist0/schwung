"""Phase 2 — MIDI_OUT stream tests.

Validates the new subscribe/dump primitives end-to-end and includes the
first real regression test for stuck notes (the original motivation for
issue #2). All tests skip cleanly if the daemon is unreachable.

A test that exercises stuck-note behavior depends on what's loaded on
Move when the test runs. Stock Move firmware emits its own note-on/
note-off pair for each pad press on tracks that are armed, so the
balance assertion (note_offs >= note_ons) is meaningful without any
schwung module loaded. With a chain slot or ion module loaded, the
test additionally catches their stuck-note regressions.
"""

from __future__ import annotations

import pytest


def test_subscribe_dump_unsubscribe_round_trip(bus):
    """Subscribe/dump/unsubscribe primitives work without errors."""
    bus.subscribe_midi_out()
    bus.wait_frame(2)
    cap = bus.dump_midi_out()
    assert cap.dropped == 0
    # We don't assert event count — depends on what Move is doing
    bus.unsubscribe_midi_out()


def test_capture_context_manager(bus):
    """capture_midi_out() context manager subscribes / drains / unsubscribes."""
    with bus.capture_midi_out() as cap:
        bus.wait_frame(2)
    # cap.events populated after the with block
    assert cap.events.dropped == 0
    assert isinstance(cap.events.events, list)


def test_pad_press_emits_note_events(bus, midi_out_capture):
    """A pad press should produce at least one MIDI event on cable 0 if any
    Move track is armed (default behavior). If no events arrive at all,
    the capture path is broken regardless of which module is loaded.

    We bracket the press with a 4-frame baseline drain so a totally empty
    capture pinpoints the capture path itself (not a "no events because
    no armed track" race) — if even the baseline drain is empty AND
    nothing arrives after the press, that's a real bug we want to know.
    """
    note = 84  # mid-grid pad

    # Baseline: are we observing anything at all from the device?
    bus.wait_frame(4)
    baseline = midi_out_capture.drain()

    bus.press_pad(note, velocity=100)
    bus.wait_frame(8)
    bus.release_pad(note)
    bus.wait_frame(8)
    after = midi_out_capture.drain()

    if len(after) == 0 and len(baseline) == 0:
        pytest.fail(
            "No MIDI_OUT events seen at any point — capture path looks "
            "broken (shim not publishing, or daemon not subscribing). "
            "Verify shim is running and SHM segment exists."
        )

    if len(after) == 0:
        pytest.skip(
            "Capture path works (baseline saw events) but the pad press "
            "produced none — likely no armed track. Arm a track or load "
            "a module that emits MIDI."
        )

    notes = after.filter(cable=0, kind="note_on").events \
          + after.filter(cable=0, kind="note_off").events
    assert len(notes) > 0, (
        f"Pad press produced events but none were note-on/off on cable 0. "
        f"Got kinds: {[e.kind for e in after.events[:10]]}"
    )


def test_no_stuck_notes_after_pad_press_release(bus, midi_out_capture):
    """The regression that motivated the whole infrastructure.

    Press a pad, release it, give the system time to settle. The
    accumulated note-off count should not be less than the note-on
    count for the same note. If it is, a note got stranded — the
    voice keeps ringing on a Move track or a slot synth.
    """
    note = 84

    bus.press_pad(note, velocity=100)
    bus.wait_frame(6)
    bus.release_pad(note)
    bus.wait_frame(20)  # ample headroom for any deferred note-off

    cap = midi_out_capture.drain()
    if len(cap) == 0:
        pytest.skip(
            "No MIDI_OUT seen during press/release — likely no armed "
            "track. This test needs at least one armed Move track or a "
            "loaded module that emits notes."
        )

    note_ons  = cap.filter(kind="note_on",  note=note).events
    note_offs = cap.filter(kind="note_off", note=note).events

    # Note-on with velocity 0 is a logical note-off (running-status
    # convention some firmwares use).
    implicit_offs = [e for e in note_ons if e.data2 == 0]
    real_ons      = [e for e in note_ons if e.data2 > 0]

    total_offs = len(note_offs) + len(implicit_offs)
    total_ons  = len(real_ons)

    assert total_offs >= total_ons, (
        f"stuck note {note}: {total_ons} note-on(s) but only "
        f"{total_offs} note-off(s) — voice will keep ringing.\n"
        f"events: {[(e.kind, e.data1, e.data2) for e in cap.events]}"
    )


def test_track_switch_does_not_strand_notes(bus, midi_out_capture):
    """Pressing a pad on one track, then switching to another track,
    should not leave a stuck note on the original track. This is the
    specific symptom the user keeps hitting in ion development."""
    pad_t1 = 92  # track 1 pad A
    pad_t4 = 68  # track 4 pad A

    bus.press_pad(pad_t1, velocity=100)
    bus.wait_frame(6)
    bus.release_pad(pad_t1)
    bus.wait_frame(6)

    # Simulate track-button press by injecting CC 40 (Track 4 per
    # CLAUDE.md: "Tracks: CCs 40-43, reversed: CC43=Track1, CC40=Track4").
    # Hold for 8 frames (~23 ms) so Move's UI poller sees a real press
    # rather than a debounced bounce.
    bus.inject_midi(bytes([0x0B, 0xB0, 40, 127]))  # CC 40 (Track 4) on
    bus.wait_frame(8)
    bus.inject_midi(bytes([0x0B, 0xB0, 40, 0]))    # CC 40 release
    bus.wait_frame(10)

    bus.press_pad(pad_t4, velocity=100)
    bus.wait_frame(6)
    bus.release_pad(pad_t4)
    bus.wait_frame(20)

    cap = midi_out_capture.drain()
    if len(cap) == 0:
        pytest.skip("No MIDI_OUT — no armed tracks?")

    for n in (pad_t1, pad_t4):
        ons  = [e for e in cap.filter(kind="note_on",  note=n).events if e.data2 > 0]
        offs = (cap.filter(kind="note_off", note=n).events
                + [e for e in cap.filter(kind="note_on", note=n).events if e.data2 == 0])
        assert len(offs) >= len(ons), (
            f"stuck note {n} after track switch: {len(ons)} on(s), "
            f"{len(offs)} off(s).\n"
            f"events: {[(e.kind, e.data1, e.data2, f'@{e.frame}') for e in cap.events]}"
        )
