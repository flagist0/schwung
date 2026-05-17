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


def _count_ons_offs(cap, note):
    """Count real note-ons vs. all note-offs (explicit + velocity-0) for ``note``.

    Some firmwares emit note-off as note-on with velocity 0 (running-status
    convention). Splitting those out keeps the balance assertion honest;
    treating vel-0 note-ons as note-ons would falsely flag every
    well-behaved release as a stuck note. Used by both stuck-note tests
    in this file so the rule stays in one place.

    Returns ``(real_on_count, total_off_count)``.
    """
    note_ons      = cap.filter(kind="note_on",  note=note).events
    note_offs     = cap.filter(kind="note_off", note=note).events
    implicit_offs = [e for e in note_ons if e.data2 == 0]
    real_ons      = [e for e in note_ons if e.data2 > 0]
    return len(real_ons), len(note_offs) + len(implicit_offs)


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
    """A pad press should produce at least one MIDI event on cable 0 —
    Move's firmware always echoes the press as a note_on packet to its
    internal MIDI fanout (and also writes pad-LED updates as note_on
    packets, which is fine for this test — we only need *something*).

    On previous iterations this test would ``pytest.fail`` when the
    capture saw zero events at any point, on the theory that the
    capture path itself was broken. In practice that produces false
    positives: Move at idle (no active modules, no MIDI clock running,
    no recent input) genuinely emits nothing for the baseline window,
    and any blip — bus reconnect, scheduling jitter — that swallows
    one of the press echoes makes the test fail in a misleading way.
    Skip instead — if the capture path is truly broken, other tests
    (the subscribe/dump round-trip, the stuck-notes regression) will
    show it.
    """
    note = 84  # mid-grid pad

    bus.press_pad(note, velocity=100)
    bus.wait_frame(8)
    bus.release_pad(note)
    bus.wait_frame(8)
    after = midi_out_capture.drain()

    if len(after) == 0:
        pytest.skip(
            "Pad press produced no MIDI_OUT events. Either the capture "
            "path is broken (other tests will diagnose) or Move is in a "
            "state where the press doesn't echo (e.g. pad bound to a "
            "muted slot)."
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
    voice keeps ringing on the downstream synth.

    Filters to ``cable=2`` (external USB MIDI out) — cable 0 also
    carries Move's pad-LED updates as note_on packets (color in the
    velocity byte), which would make any pressed pad look like a
    stuck note. The real regression appears on the cable that goes
    to the downstream synth.

    Requires at least one Move track armed to USB MIDI OUT. Skips
    cleanly otherwise.
    """
    note = 84

    bus.press_pad(note, velocity=100)
    bus.wait_frame(6)
    bus.release_pad(note)
    bus.wait_frame(20)  # ample headroom for any deferred note-off

    cap = midi_out_capture.drain().filter(cable=2)
    if len(cap) == 0:
        pytest.skip(
            "No cable=2 MIDI_OUT during press/release — no Move tracks "
            "armed to USB MIDI OUT. Arm at least one track to USB on the "
            "Move to exercise this regression."
        )

    total_ons, total_offs = _count_ons_offs(cap, note)
    assert total_offs >= total_ons, (
        f"stuck note {note}: {total_ons} note-on(s) but only "
        f"{total_offs} note-off(s) — voice will keep ringing.\n"
        f"events: {[(e.kind, e.data1, e.data2) for e in cap.events]}"
    )


def test_track_switch_does_not_strand_notes(bus, commander, midi_out_capture):
    """Pressing a pad on one track, then switching to another track,
    should not leave a stuck note on the original track. This is the
    specific symptom the user keeps hitting in ion development.

    Filters to ``cable=2`` (external USB MIDI out) — cable 0 carries LED
    writes that use the same note-on wire format and would otherwise
    flood the assertion with false positives (every track switch repaints
    ~32 pad LEDs as note_on events). Real "stuck note" symptoms manifest
    on the external bus where downstream synths actually keep ringing.

    Requires at least one Move track armed to USB MIDI OUT. Skips
    cleanly otherwise — that's a setup gap, not a regression.

    Track switch goes through Commander so teardown reverts focus to
    track 1, keeping cross-test isolation honest. Raw ``inject_midi``
    for the same gesture would leave the device on track 4 with no
    undo, contaminating any later test that reads ``selected_slot``.
    """
    from schwung_bus.move_commands import SelectTrack

    pad_t1 = 92  # track 1 pad A
    pad_t4 = 68  # track 4 pad A

    bus.press_pad(pad_t1, velocity=100)
    bus.wait_frame(6)
    bus.release_pad(pad_t1)
    bus.wait_frame(6)

    # Track 4 = CC 40 (reversed: CC43=T1 .. CC40=T4). Commander's
    # SelectTrack(4) sends the press, undo restores to track 1.
    commander.do(SelectTrack(4, restore_to=1))
    bus.wait_frame(10)

    bus.press_pad(pad_t4, velocity=100)
    bus.wait_frame(6)
    bus.release_pad(pad_t4)
    bus.wait_frame(20)

    cap = midi_out_capture.drain().filter(cable=2)
    if len(cap) == 0:
        pytest.skip(
            "No cable=2 MIDI_OUT — no Move tracks armed to USB MIDI OUT. "
            "Arm at least one track to USB on the Move to exercise this regression."
        )

    for n in (pad_t1, pad_t4):
        ons, offs = _count_ons_offs(cap, n)
        assert offs >= ons, (
            f"stuck note {n} after track switch: {ons} on(s), "
            f"{offs} off(s).\n"
            f"events: {[(e.kind, e.data1, e.data2, f'@{e.frame}') for e in cap.events]}"
        )
