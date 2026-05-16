"""Pytest entry point for pytest-schwung.

Fixtures:
  ``bus``               session-scoped SchwungBus, connected and
                        ping-validated. Auto-skips collected tests if
                        the daemon is unreachable.
  ``midi_out_capture``  function-scoped capture of MIDI_OUT events that
                        happened during the test body. Yields a
                        MidiOutCaptureContext whose `.events` is populated
                        after the test runs — but tests can also use the
                        context's bus methods directly for finer control.
"""

from __future__ import annotations

import socket

import pytest

from .client import SchwungBus, SchwungBusError, MidiOutCaptureContext


@pytest.fixture(scope="session")
def bus() -> SchwungBus:
    b = SchwungBus()
    try:
        b.connect()
        b.ping()  # confirm protocol handshake works, not just TCP accept
    except (OSError, socket.timeout, SchwungBusError) as e:
        pytest.skip(
            f"schwung-testd unreachable at {b.host}:{b.port} ({e}). "
            "Start the daemon on Move and tunnel the port."
        )
    yield b
    b.close()


@pytest.fixture
def midi_out_capture(bus) -> MidiOutCaptureContext:
    """Capture MIDI_OUT events emitted during the test.

    Tests treat this as a live handle to the capture: events are not yet
    populated when the fixture is entered; they're populated when the
    test calls `cap.events = bus.dump_midi_out()` explicitly, OR when
    the fixture's teardown drains automatically.

    Typical use::

        def test_no_stuck_notes(bus, midi_out_capture):
            bus.press_pad(84); bus.wait_frame(8)
            bus.release_pad(84); bus.wait_frame(8)
            cap = bus.dump_midi_out()  # snapshot before exit
            assert len(cap.filter(kind="note_off")) >= len(cap.filter(kind="note_on"))
    """
    bus.subscribe_midi_out()
    ctx = MidiOutCaptureContext(bus)
    ctx._bus = bus  # already subscribed; suppress re-subscribe on enter
    try:
        yield ctx
    finally:
        try:
            ctx.events = bus.dump_midi_out()
        except Exception:
            pass
        try:
            bus.unsubscribe_midi_out()
        except Exception:
            pass
