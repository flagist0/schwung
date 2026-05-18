"""Shim frame-counter invariants.

``shim_counter`` is incremented every SPI tick (~344 Hz on Move
hardware) by ``shadow_mix_audio()``. It's the canonical wall-clock
proxy for tests — wait_frame(N) uses it, MidiOutEvent.frame stamps
it, fresh_move's freeze-detect compares it.

If the counter ever wraps, reverses, freezes outside restart-move, or
runs at a noticeably wrong rate, every test that depends on frame-
ordered assertions silently misbehaves. These tests pin the contract.

test_smoke.test_wait_frame_advances_counter already covers the
"two calls in a row → second >= first + N" case for N=1, 2. The
tests here cover:
  - monotonicity across many samples (not just two)
  - rate matches ~344 Hz within reasonable tolerance
  - rate stays right under STATE/snapshot/inject churn
"""

from __future__ import annotations

import time


# Move's SPI tick rate per docs/SPI_PROTOCOL.md and CLAUDE.md.
SHIM_TICK_HZ = 344
# Allow ±20% to absorb USB-Ethernet latency, daemon serve time, and
# the Linux scheduler. Tighter bounds would flake on slow hosts.
SHIM_TICK_TOLERANCE = 0.20


def test_counter_strictly_monotonic_over_many_samples(bus):
    """Sample shim_counter 30 times back-to-back with small gaps.
    Each sample must advance forward — never reverse. A reverse means
    either the SHM is being read torn (32-bit write seen as two
    halves), or the counter is reset under our feet by some unrelated
    handler.

    Wrap handling: ``shim_counter`` is uint32 (~14 days at 344 Hz).
    A natural wrap looks like ``small_new - large_old`` in raw Python
    integers, which would falsely match "went backwards". Use the
    masked delta — any positive forward advance modulo 2^32 is fine;
    only flag a huge backward jump (delta close to 0xFFFFFFFF).
    """
    samples = []
    for _ in range(30):
        samples.append(bus.state().shim_counter)
        # No wait_frame — sample as fast as TCP round-trip allows.
        # Wall-clock between samples is ~5-15 ms.

    for i in range(1, len(samples)):
        delta = (samples[i] - samples[i - 1]) & 0xFFFFFFFF
        # Normal forward advance: delta is small (a few ticks).
        # Wrap-through-zero: delta is small (the wrap is forward motion).
        # Real reversal: delta is huge (~2^32 minus a tiny gap).
        # 0x80000000 = half the 32-bit range, generous boundary.
        assert delta < 0x80000000, (
            f"shim_counter went backwards between samples "
            f"{i-1} and {i}: {samples[i-1]} → {samples[i]} "
            f"(masked delta={delta:#010x}).\n"
            f"All samples: {samples}"
        )

    # Sanity: counter actually moved over the ~150-450 ms window.
    total_delta = (samples[-1] - samples[0]) & 0xFFFFFFFF
    assert total_delta > 0, (
        f"shim_counter never advanced across 30 samples — shim "
        f"frozen? First={samples[0]}, last={samples[-1]}"
    )


def test_counter_rate_matches_spi_tick_rate(bus):
    """Measure counter advance per wall-clock second over a 1-second
    window; should be within ±20% of 344 Hz. A wildly wrong rate
    points at the audio thread being broken (counter not advancing)
    or running at the wrong sample rate.

    Window is 1 s on purpose: shorter and scheduler jitter dominates;
    longer and the test is slow without adding signal.
    """
    t0 = time.monotonic()
    c0 = bus.state().shim_counter
    # Single wait_frame(1) ensures the shim is alive (returns
    # immediately on the next tick) and gives us a clean start point.
    bus.wait_frame(1)

    # Settle then measure
    t_start = time.monotonic()
    c_start = bus.state().shim_counter

    # Sleep close to 1 second on the wall clock.
    target = 1.0
    time.sleep(target)

    t_end = time.monotonic()
    c_end = bus.state().shim_counter

    elapsed = t_end - t_start
    # Masked delta — see test_counter_strictly_monotonic for rationale.
    # Without the mask a wrap during the 1-second window would yield
    # ticks = large negative Python int, then measured_hz is negative,
    # and the bounds check rejects it as a rate error rather than a wrap.
    ticks = (c_end - c_start) & 0xFFFFFFFF
    measured_hz = ticks / elapsed

    lo = SHIM_TICK_HZ * (1 - SHIM_TICK_TOLERANCE)
    hi = SHIM_TICK_HZ * (1 + SHIM_TICK_TOLERANCE)
    assert lo <= measured_hz <= hi, (
        f"shim tick rate out of range: measured {measured_hz:.1f} Hz "
        f"over {elapsed:.3f}s ({ticks} ticks), expected "
        f"{SHIM_TICK_HZ} Hz ±{SHIM_TICK_TOLERANCE*100:.0f}% "
        f"([{lo:.0f}, {hi:.0f}]).\n"
        f"warmup: {c0} @ {t0:.3f}; start: {c_start} @ {t_start:.3f}; "
        f"end: {c_end} @ {t_end:.3f}"
    )


def test_counter_advances_during_inject_load(bus):
    """Hammer the inject ring with 20 packets in a tight loop and
    confirm the counter advanced normally during that window. Catches
    a regression where heavy inject causes the audio thread to stall
    (e.g., a lock contention regression in the inject ring drain).
    """
    # Sync to a frame boundary before measuring so the start point
    # isn't an artifact of "where the last test left us". Otherwise
    # the inject loop's measurement window can collapse on a slow
    # host (STATE + 20 INJECTs taking longer than 4 frames) and the
    # `max(1, ...)` floor lets a stalled audio thread pass.
    bus.wait_frame(1)
    c_start = bus.state().shim_counter
    t_start = time.monotonic()

    # 20 NOP CCs on cable 0 — well below ring capacity (63 packets per
    # the cursor cap in shadow_drain_midi_inject) but enough to stress
    # the drain path multiple times.
    for i in range(20):
        # CC 7 (volume) on channel 0 — Move ignores it on cable 0
        # without a track armed; chosen because it's the canonical
        # "harmless CC" for tests.
        bus.inject_midi(bytes([0x0B, 0xB0, 7, i % 128]))

    # 8 frames (~23 ms) gives the audio thread real wall-clock time
    # to advance even if the inject loop took a couple of frames.
    bus.wait_frame(8)
    c_end = bus.state().shim_counter
    elapsed = time.monotonic() - t_start

    # Masked delta — see test_counter_strictly_monotonic. Without it
    # a wrap turns the assertion into a wrong-but-not-crashing pass
    # (huge negative ticks satisfy `>= expected_min` trivially).
    ticks = (c_end - c_start) & 0xFFFFFFFF
    # Expect at least `elapsed * 344Hz * 0.5` ticks (half the nominal
    # rate is the floor we tolerate before we call it stalled).
    expected_min = max(1, int(elapsed * SHIM_TICK_HZ * 0.5))
    assert ticks >= expected_min, (
        f"counter advanced only {ticks} ticks over {elapsed:.3f}s of "
        f"inject load — expected at least {expected_min} (half nominal "
        f"rate). Inject path may be stalling the audio thread."
    )
