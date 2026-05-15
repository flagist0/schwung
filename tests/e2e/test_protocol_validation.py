"""Protocol-validation tests: assert the daemon rejects malformed input.

These don't change device state — they exercise paths that should return
``ERR`` without touching the inject ring or blocking on frames. The intent
is to catch regressions where a validation guard is dropped or weakened
(e.g. ``>`` for a bounds check that should be ``>=``, ``startswith`` for
something that should be exact-match).
"""

from __future__ import annotations

import pytest

from schwung_bus import SchwungBusError


def test_inject_midi_rejects_bad_hex(bus):
    with pytest.raises(SchwungBusError, match="bad hex"):
        bus._request("INJECT_MIDI ZZZZZZZZ")


def test_inject_midi_rejects_odd_length_hex(bus):
    with pytest.raises(SchwungBusError, match="8 hex chars"):
        bus._request("INJECT_MIDI 0BB0307")


def test_inject_midi_rejects_short_hex(bus):
    with pytest.raises(SchwungBusError, match="8 hex chars"):
        bus._request("INJECT_MIDI 0B")


def test_wait_frame_rejects_zero(bus):
    with pytest.raises(SchwungBusError, match="N must be"):
        bus._request("WAIT_FRAME 0")


def test_wait_frame_rejects_negative(bus):
    with pytest.raises(SchwungBusError, match="N must be"):
        bus._request("WAIT_FRAME -1")


def test_wait_frame_rejects_over_max(bus):
    with pytest.raises(SchwungBusError, match="N must be"):
        bus._request("WAIT_FRAME 99999")


def test_wait_frame_rejects_trailing_junk(bus):
    # 'WAIT_FRAME 5junk' must not be silently accepted as 5.
    with pytest.raises(SchwungBusError, match="N must be"):
        bus._request("WAIT_FRAME 5junk")


def test_unknown_verb(bus):
    with pytest.raises(SchwungBusError, match="unknown command"):
        bus._request("FROBNICATE")


def test_ping_rejects_args(bus):
    # Typo'd commands should fail loudly, not silently degrade to PING.
    with pytest.raises(SchwungBusError, match="no args"):
        bus._request("PING extra")


def test_snapshot_pad_leds_rejects_args(bus):
    with pytest.raises(SchwungBusError, match="no args"):
        bus._request("SNAPSHOT_PAD_LEDS junk")
