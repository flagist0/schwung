"""MPSC inject-ring burst-load tests.

``/schwung-midi-inject`` is a lock-free MPSC ring (see
``src/host/shadow_midi_inject_writer.h`` and
``src/host/shadow_midi.c::shadow_drain_midi_inject``). Its drain
rate is 16 packets per SPI frame (≈5500 packets/sec), capacity is
63 packets (252 usable bytes / 4 bytes per packet; ring buffer
itself is 256 bytes but the producer guard caps at slot 252), with
carryover for packets that didn't fit in one drain. A 2-frame defer
guard suppresses drain when hardware MIDI is present in the shadow
buffer, so settle waits must be generous.

Tests pin four properties:

  1. **No drop under realistic burst** — a small fast burst all lands;
     final observable state reflects the LAST event.
  2. **Carryover survives drain rate** — bursts that exceed the
     16-per-frame drain still land all events. Catches a regression
     where carryover writes back to the wrong cursor or drops the tail.
  3. **Ring full doesn't crash** — injecting beyond capacity returns
     silently from the producer; the shim continues to tick.
  4. **Daemon stays responsive after a burst** — STATE returns
     quickly. Catches inject-ring deadlocks where the audio thread
     or the daemon stalls.

These are silent tests: only CC 7 (harmless on stock Move without
an armed track) and track CCs are injected. No pad notes.
"""

from __future__ import annotations

import time

import pytest

from schwung_bus.move_commands import SelectTrack


# Match the constants in shadow_midi.c. If those change, update here.
DRAIN_RATE_PER_FRAME = 16
RING_CAPACITY_PACKETS = 63  # 252-byte producer cap / 4 bytes per packet

# Settle window for inject-drain to complete, with margin for the
# 2-frame defer guard that fires on hardware MIDI activity. Used as
# the standard wait after any inject burst in this file.
SETTLE_FRAMES = 20


def _press_release_track(bus, cc: int) -> None:
    """Raw press+release for a track CC, no wait_frame between.

    Tests in this file deliberately bypass Commander to get raw
    burst behavior without Commander's built-in 2/4-frame settles.
    """
    bus.inject_midi(bytes([0x0B, 0xB0, cc, 127]))
    bus.inject_midi(bytes([0x0B, 0xB0, cc, 0]))


def _restore_track1(bus) -> None:
    """Press track 1 to reset selected_slot for the next test.

    Used in finally blocks so that even if the inject test assertion
    fails, the next test doesn't inherit selected_slot=3.
    """
    _press_release_track(bus, 43)  # CC 43 = Track 1
    bus.wait_frame(4)


def test_small_burst_lands_on_last_track(bus, commander):
    """Inject T1→T2→T3→T4 press+release in tight succession (8
    packets total) and confirm ``selected_slot`` ends at 3 (track 4
    = CC 40 = slot 3). If the ring drops any of the four track
    presses, slot lands somewhere else.
    """
    commander.do(SelectTrack(1, restore_to=1))
    bus.wait_frame(4)
    assert bus.state().selected_slot == 0, "test setup failed"

    try:
        # Raw burst: 4 track presses + releases, no inter-event waits.
        # Order T1→T2→T3→T4 means CC sequence 43, 42, 41, 40.
        for cc in (43, 42, 41, 40):
            _press_release_track(bus, cc)

        bus.wait_frame(SETTLE_FRAMES)
        s = bus.state()
        assert s.selected_slot == 3, (
            f"after burst T1→T2→T3→T4, selected_slot is {s.selected_slot} "
            f"(expected 3). The inject ring may have dropped or reordered "
            f"track events under burst load."
        )
    finally:
        # Restore even on assert failure — otherwise the next test
        # inherits selected_slot=3 and its setup assert fires with a
        # misleading "test setup failed" message that hides this failure.
        _restore_track1(bus)


def test_burst_above_drain_rate_carries_over(bus, commander):
    """Inject (drain_rate + 2) CCs in one shot then a track-4 marker.
    The first frame drains 16 packets, leaving 2 to carry over to
    the next frame, then the marker. If the carryover path is broken
    the marker is the one that gets lost (LIFO drop) and slot stays
    at 0.

    Sizing rationale: 30+ packets drained cleanly in exactly 2
    frames with zero carryover — i.e. `> DRAIN_RATE_PER_FRAME` and
    `<= 2 * DRAIN_RATE_PER_FRAME` is the only window that actually
    exercises the `remaining > 0` carryover branch of
    ``shadow_drain_midi_inject``.
    """
    commander.do(SelectTrack(1, restore_to=1))
    bus.wait_frame(4)
    assert bus.state().selected_slot == 0, "test setup failed"

    try:
        # 18 NOPs forces 2-packet carryover after first frame's drain.
        noise_count = DRAIN_RATE_PER_FRAME + 2
        for i in range(noise_count):
            bus.inject_midi(bytes([0x0B, 0xB0, 7, i % 128]))

        # Final marker that must land after the carryover.
        _press_release_track(bus, 40)  # Track 4

        bus.wait_frame(SETTLE_FRAMES)
        s = bus.state()
        assert s.selected_slot == 3, (
            f"after {noise_count}-CC noise + track 4 press, "
            f"selected_slot is {s.selected_slot} (expected 3). "
            f"Inject ring carryover dropped the tail."
        )
    finally:
        _restore_track1(bus)


def test_ring_capacity_boundary_does_not_crash(bus, commander):
    """Inject exactly RING_CAPACITY_PACKETS (63), then one more.
    The producer guard returns -1 (drop) on the 64th packet — we
    don't observe that directly from Python (inject_midi is fire-
    and-forget), but the shim must keep ticking and the daemon
    must stay responsive.

    Pins behavior at the exact boundary where the producer's
    `my_slot + 4 >= 256` check fires. Catches a regression that
    converts the silent drop into a crash, hang, or off-by-one
    that overflows the ring.
    """
    commander.do(SelectTrack(1, restore_to=1))
    bus.wait_frame(4)
    assert bus.state().selected_slot == 0, "test setup failed"

    try:
        # Fill the ring to capacity in one frame (no wait_frame
        # between injects → shim doesn't drain).
        for i in range(RING_CAPACITY_PACKETS):
            bus.inject_midi(bytes([0x0B, 0xB0, 7, i % 128]))

        # One extra past capacity. Producer should silently drop;
        # we don't assert a return value (the bus API doesn't
        # surface it), only that the call doesn't raise/hang.
        bus.inject_midi(bytes([0x0B, 0xB0, 7, 99]))

        # After capacity-full settle, the ring should drain and the
        # final track-4 marker should land normally — confirming the
        # shim recovered from the overflow without permanently
        # losing the inject path.
        bus.wait_frame(SETTLE_FRAMES)
        _press_release_track(bus, 40)
        bus.wait_frame(SETTLE_FRAMES)

        s = bus.state()
        assert s.selected_slot == 3, (
            f"after ring-overflow event + track 4 marker, slot is "
            f"{s.selected_slot} (expected 3). The overflow path may "
            f"have permanently broken the inject ring."
        )
    finally:
        _restore_track1(bus)


def test_daemon_stays_responsive_after_burst(bus):
    """After a 100-packet burst, STATE round-trip must complete in
    under 100 ms. Healthy round-trip on the SSH loopback tunnel is
    5-15 ms; 100 ms catches any stall longer than ~6x healthy.
    Catches deadlocks where heavy inject jams either the audio
    thread or the daemon's TCP serve loop.
    """
    for i in range(100):
        bus.inject_midi(bytes([0x0B, 0xB0, 7, i % 128]))

    t0 = time.monotonic()
    bus.state()
    elapsed = time.monotonic() - t0

    assert elapsed < 0.1, (
        f"STATE round-trip took {elapsed*1000:.0f} ms after a 100-packet "
        f"burst (healthy is 5-15 ms). Daemon or shim is stalling under "
        f"inject load."
    )


def test_burst_does_not_freeze_shim_counter(bus):
    """During a 50-packet burst, the shim's frame counter must keep
    advancing. Catches a regression where heavy inject causes the
    audio thread to miss SPI frames (e.g. lock contention in the
    drain path holding back the timer).

    This is a stricter version of
    test_shim_counter.test_counter_advances_during_inject_load —
    same shape, larger burst, tighter floor.
    """
    bus.wait_frame(1)
    c_before = bus.state().shim_counter
    t_before = time.monotonic()

    for i in range(50):
        bus.inject_midi(bytes([0x0B, 0xB0, 7, i % 128]))

    bus.wait_frame(8)
    c_after = bus.state().shim_counter
    elapsed = time.monotonic() - t_before

    # Masked delta — see test_shim_counter for wrap-handling rationale.
    ticks = (c_after - c_before) & 0xFFFFFFFF
    expected_min = max(1, int(elapsed * 344 * 0.5))
    assert ticks >= expected_min, (
        f"shim_counter advanced only {ticks} ticks over {elapsed:.3f}s "
        f"during a 50-packet burst — expected at least {expected_min}. "
        f"Audio thread stalling under inject pressure."
    )
