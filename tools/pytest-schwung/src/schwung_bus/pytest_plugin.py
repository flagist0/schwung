"""Pytest entry point for pytest-schwung.

Fixtures:
  ``bus``               session-scoped SchwungBus, connected and
                        ping-validated. Auto-skips collected tests if
                        the daemon is unreachable.
  ``midi_out_capture``  function-scoped MidiOutSession. The fixture
                        subscribes on setup, yields a session handle,
                        unsubscribes on teardown. Tests call
                        ``session.drain()`` to read captured events
                        (multiple times in one test is fine — each drain
                        returns events since the last).
"""

from __future__ import annotations

import socket

import pytest

from .client import SchwungBus, SchwungBusError, MidiOutSession
from .commander import Commander


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
def commander(bus) -> Commander:
    """Command-pattern stack for UI tests.

    Yields a Commander. Tests build state by calling ``commander.do(cmd)``;
    the fixture's teardown calls ``commander.undo_all()`` to reverse
    every action in LIFO order — even if the test failed mid-way.

    See ``schwung_bus.move_commands`` for concrete commands.
    """
    c = Commander(bus=bus)
    try:
        yield c
    finally:
        c.undo_all()


@pytest.fixture
def midi_out_capture(bus) -> MidiOutSession:
    """Subscribe to MIDI_OUT events for the duration of one test.

    Yields a MidiOutSession. Call ``session.drain()`` (or the equivalent
    shorter ``session()``) to read events captured since the last drain
    (or since subscribe). The fixture handles unsubscribe on teardown,
    so failing tests don't leak the subscription into the next test.

    Typical use::

        def test_no_stuck_notes(bus, midi_out_capture):
            bus.press_pad(84); bus.wait_frame(8)
            bus.release_pad(84); bus.wait_frame(8)
            cap = midi_out_capture.drain()
            assert len(cap.filter(kind="note_off")) >= len(cap.filter(kind="note_on"))
    """
    bus.subscribe_midi_out()
    try:
        yield MidiOutSession(bus)
    finally:
        try:
            bus.unsubscribe_midi_out()
        except Exception:
            pass
