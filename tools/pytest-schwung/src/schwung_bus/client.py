"""TCP client for schwung-testd, the on-device test-bus daemon.

Wraps the line protocol (PING / INJECT_MIDI / WAIT_FRAME /
SNAPSHOT_PAD_LEDS / SUBSCRIBE_MIDI_OUT / DUMP_MIDI_OUT /
UNSUBSCRIBE_MIDI_OUT / QUIT) and exposes both raw primitives and
semantic helpers (press_pad, release_pad, capture_midi_out). The daemon
listens on TCP loopback by default; reach it from a dev machine via
`ssh -L 47777:localhost:47777`.

Sequential, single-connection — matches the Phase 1 daemon, which
accepts one client at a time. Threading and async are out of scope.
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass, field
from typing import Optional, List

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 47777


class SchwungBusError(RuntimeError):
    """Raised when the daemon returns an ERR response or the protocol breaks."""


@dataclass
class WaitFrameResult:
    counter: int


@dataclass
class MidiOutEvent:
    """One USB-MIDI packet observed in Move's MIDI_OUT by the shim."""
    frame: int       # shim_counter at capture time
    cable: int       # 0=internal, 2=external USB
    cin: int         # USB-MIDI Code Index Number (0x9=note-on, 0x8=note-off, 0xB=CC...)
    status: int      # MIDI status byte (0x90, 0x80, 0xB0, ...)
    data1: int       # note / CC#
    data2: int       # velocity / value

    @property
    def channel(self) -> int:
        return self.status & 0x0F

    @property
    def kind(self) -> str:
        """Human-readable event kind. Matches the `status=...` filter values."""
        hi = self.status & 0xF0
        return {
            0x80: "note_off",
            0x90: "note_on",
            0xA0: "aftertouch",
            0xB0: "cc",
            0xC0: "program_change",
            0xD0: "channel_pressure",
            0xE0: "pitch_bend",
            0xF0: "system",
        }.get(hi, "unknown")


@dataclass
class MidiOutCapture:
    """A snapshot of MIDI_OUT events drained from the daemon.

    Returned by `bus.dump_midi_out()`. Supports simple filtering helpers
    so tests read naturally:

        cap = bus.dump_midi_out()
        ons  = cap.filter(kind="note_on")
        offs = cap.filter(kind="note_off", note=60)
        assert len(offs) >= len(ons), "stuck notes"
    """
    events: List[MidiOutEvent] = field(default_factory=list)
    dropped: int = 0  # events that overflowed the ring before we drained

    def __len__(self) -> int:
        return len(self.events)

    def __iter__(self):
        return iter(self.events)

    def filter(
        self,
        kind: Optional[str] = None,
        cable: Optional[int] = None,
        channel: Optional[int] = None,
        note: Optional[int] = None,
        cc: Optional[int] = None,
    ) -> "MidiOutCapture":
        """Return a new capture with only events matching all given keys.

        `note` filters data1 for note-on/off/aftertouch events.
        `cc` filters data1 for control-change events.
        """
        def keep(e: MidiOutEvent) -> bool:
            if kind is not None and e.kind != kind: return False
            if cable is not None and e.cable != cable: return False
            if channel is not None and e.channel != channel: return False
            if note is not None and e.data1 != note: return False
            if cc is not None and e.data1 != cc: return False
            return True
        return MidiOutCapture(events=[e for e in self.events if keep(e)], dropped=0)


class SchwungBus:
    """Synchronous client for one schwung-testd instance.

    Use as a context manager or call ``connect()`` / ``close()`` explicitly.
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        connect_timeout: float = 2.0,
        recv_timeout: float = 35.0,  # > daemon's 30s WAIT_FRAME cap
    ) -> None:
        self.host = host or os.environ.get("SCHWUNG_TEST_HOST", DEFAULT_HOST)
        env_port = os.environ.get("SCHWUNG_TEST_PORT")
        self.port = port if port is not None else (int(env_port) if env_port else DEFAULT_PORT)
        self._connect_timeout = connect_timeout
        self._recv_timeout = recv_timeout
        self._sock: Optional[socket.socket] = None
        self._buf = bytearray()

    def connect(self) -> None:
        if self._sock is not None:
            return
        s = socket.create_connection((self.host, self.port), timeout=self._connect_timeout)
        s.settimeout(self._recv_timeout)
        self._sock = s

    def close(self) -> None:
        if self._sock is None:
            return
        try:
            self._send_line("QUIT")
            self._read_line()  # consume "OK bye"
        except Exception:
            pass
        try:
            self._sock.close()
        finally:
            self._sock = None
            self._buf.clear()

    def __enter__(self) -> "SchwungBus":
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ----- raw protocol primitives ------------------------------------------

    def ping(self) -> str:
        """Return the daemon's identity string (e.g. 'schwung-testd 0.1.0')."""
        return self._request("PING")

    def inject_midi(self, packet: bytes) -> None:
        """Inject one 4-byte USB-MIDI packet into Move's MIDI_IN buffer.

        Packet format: [CIN+cable, status, data1, data2]. Cable nibble is
        the high nibble of byte 0; CIN the low nibble. Cable 0 = internal
        hardware (pads/buttons), cable 2 = external USB.
        """
        if len(packet) != 4:
            raise ValueError(f"INJECT_MIDI expects exactly 4 bytes, got {len(packet)}")
        self._request("INJECT_MIDI " + packet.hex())

    def wait_frame(self, n: int = 1) -> WaitFrameResult:
        """Block until the shim has ticked at least N more SPI frames."""
        if n < 1:
            raise ValueError("wait_frame N must be >= 1")
        line = self._request(f"WAIT_FRAME {n}")
        # Reply is "frame=<counter>" after the OK prefix is stripped
        if not line.startswith("frame="):
            raise SchwungBusError(f"unexpected WAIT_FRAME reply: {line!r}")
        try:
            counter = int(line.split("=", 1)[1])
        except ValueError as e:
            raise SchwungBusError(f"unparseable WAIT_FRAME counter: {line!r}") from e
        if counter < 0:
            raise SchwungBusError(f"negative frame counter: {counter}")
        return WaitFrameResult(counter=counter)

    def snapshot_pad_leds(self) -> bytes:
        """Return the current 32-byte pad LED color snapshot.

        Index 0 = note 68 (track 4 pad A), ..., index 31 = note 99 (track 1
        pad H). Each byte is a Move LED color code (0 = off).
        """
        line = self._request("SNAPSHOT_PAD_LEDS")
        try:
            data = bytes.fromhex(line)
        except ValueError as e:
            raise SchwungBusError(f"bad SNAPSHOT_PAD_LEDS hex: {line!r}") from e
        if len(data) != 32:
            raise SchwungBusError(f"expected 32 LED bytes, got {len(data)}")
        return data

    # ----- MIDI_OUT stream subscription (Phase 2) ---------------------------

    def subscribe_midi_out(self) -> None:
        """Start capturing MIDI_OUT events. Resets the per-subscriber
        baseline so the next `dump_midi_out()` returns events from now.

        The shim's capture path stays on until `unsubscribe_midi_out()`
        (or QUIT / connection close) — the cost when idle is one atomic
        load + branch per SPI frame, negligible.
        """
        self._request("SUBSCRIBE_MIDI_OUT")

    def unsubscribe_midi_out(self) -> None:
        """Stop capturing MIDI_OUT events in the shim."""
        self._request("UNSUBSCRIBE_MIDI_OUT")

    def dump_midi_out(self) -> MidiOutCapture:
        """Drain MIDI_OUT events captured since the last subscribe/dump.

        Advances the baseline so the next call returns only new events.
        `dropped` is non-zero if the ring overflowed (1024 events fit) —
        bump TEST_STREAM_CAPACITY in shadow_constants.h if you hit it.

        Wraps every parser failure in SchwungBusError (not raw ValueError)
        so callers get a consistent exception type and a message that
        names what went wrong on the wire.
        """
        if self._sock is None:
            raise SchwungBusError("bus not connected")
        self._send_line("DUMP_MIDI_OUT")
        header = self._read_line()
        if not (header == "OK" or header.startswith("OK ")):
            if header.startswith("ERR"):
                raise SchwungBusError(header[3:].lstrip() or "ERR (no message)")
            raise SchwungBusError(f"unexpected DUMP_MIDI_OUT reply: {header!r}")

        # Parse "OK count=N dropped=D" — count is required.
        rest = header[2:].lstrip()
        count: Optional[int] = None
        dropped = 0
        for tok in rest.split():
            if tok.startswith("count="):
                try:
                    count = int(tok[6:])
                except ValueError as e:
                    raise SchwungBusError(f"DUMP_MIDI_OUT bad count token: {tok!r}") from e
            elif tok.startswith("dropped="):
                try:
                    dropped = int(tok[8:])
                except ValueError as e:
                    raise SchwungBusError(f"DUMP_MIDI_OUT bad dropped token: {tok!r}") from e
        if count is None:
            raise SchwungBusError(f"DUMP_MIDI_OUT header missing count=: {header!r}")
        if count < 0 or dropped < 0:
            raise SchwungBusError(f"DUMP_MIDI_OUT negative count/dropped: {header!r}")

        events: List[MidiOutEvent] = []
        for _ in range(count):
            line = self._read_line()
            if not line.startswith("EV "):
                raise SchwungBusError(f"expected EV line, got {line!r}")
            parts = line.split()
            if len(parts) != 3:
                raise SchwungBusError(
                    f"malformed EV line (expected 'EV <frame> <pkt>'): {line!r}"
                )
            _, frame_hex, pkt_hex = parts
            try:
                frame = int(frame_hex, 16)
                pkt = bytes.fromhex(pkt_hex)
            except ValueError as e:
                raise SchwungBusError(f"bad EV hex in {line!r}: {e}") from e
            if len(pkt) != 4:
                raise SchwungBusError(f"EV packet must be 4 bytes, got {len(pkt)}")
            events.append(MidiOutEvent(
                frame=frame,
                cable=(pkt[0] >> 4) & 0x0F,
                cin=pkt[0] & 0x0F,
                status=pkt[1],
                data1=pkt[2],
                data2=pkt[3],
            ))
        end = self._read_line()
        if end != "END":
            raise SchwungBusError(f"expected END, got {end!r}")
        return MidiOutCapture(events=events, dropped=dropped)

    def capture_midi_out(self) -> "MidiOutCaptureContext":
        """Context manager: subscribe on enter, dump on exit.

        Usage::

            with bus.capture_midi_out() as cap:
                bus.press_pad(84)
                bus.wait_frame(8)
            # cap.events populated, subscription closed
        """
        return MidiOutCaptureContext(self)

    # ----- semantic helpers --------------------------------------------------

    def press_pad(self, note: int, velocity: int = 100) -> None:
        """Inject a note-on for a pad (notes 68..99) on cable 0, channel 0."""
        _check_pad_note(note)
        if not 1 <= velocity <= 127:
            raise ValueError("velocity must be 1..127 for note-on")
        # Cable=0 (high nibble), CIN=9 (note-on, low nibble) -> 0x09
        self.inject_midi(bytes([0x09, 0x90, note, velocity]))

    def release_pad(self, note: int, velocity: int = 0x40) -> None:
        """Inject a note-off for a pad on cable 0, channel 0.

        ``velocity`` is the release velocity (0..127). Default 0x40 matches
        the standard "no release-velocity sensor" value; pass a real
        velocity if the test exercises a release-velocity-aware module.
        """
        _check_pad_note(note)
        if not 0 <= velocity <= 127:
            raise ValueError("release velocity must be 0..127")
        # CIN=8 (note-off), status 0x80
        self.inject_midi(bytes([0x08, 0x80, note, velocity]))

    def pad_index(self, note: int) -> int:
        """Convert a pad note (68..99) to its pad_led_colors index (0..31)."""
        _check_pad_note(note)
        return note - 68

    # ----- internals ---------------------------------------------------------

    def _request(self, command: str) -> str:
        """Send one command line and return the OK response payload (no prefix).

        Raises ``SchwungBusError`` on ERR responses, malformed lines, or
        connection problems.
        """
        if self._sock is None:
            raise SchwungBusError("bus not connected")
        self._send_line(command)
        line = self._read_line()
        # Token-match (not prefix-match) so a hypothetical "OKAY foo" or
        # "ERROR bad" reply doesn't get silently mis-routed as success/error.
        if line == "OK" or line.startswith("OK "):
            return line[2:].lstrip()
        if line == "ERR" or line.startswith("ERR "):
            return self._raise_err(line[3:].lstrip())
        raise SchwungBusError(f"protocol error: unexpected reply {line!r}")

    @staticmethod
    def _raise_err(msg: str) -> str:
        raise SchwungBusError(msg or "ERR (no message)")

    def _send_line(self, line: str) -> None:
        assert self._sock is not None
        self._sock.sendall((line + "\n").encode("ascii"))

    def _read_line(self) -> str:
        assert self._sock is not None
        # Drain any buffered bytes first
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line = bytes(self._buf[:nl]).decode("ascii", errors="replace").rstrip("\r")
                del self._buf[: nl + 1]
                return line
            chunk = self._sock.recv(4096)
            if not chunk:
                raise SchwungBusError("connection closed by daemon")
            self._buf.extend(chunk)


def _check_pad_note(note: int) -> None:
    if not 68 <= note <= 99:
        raise ValueError(f"pad note must be 68..99, got {note}")


class MidiOutSession:
    """Live MIDI_OUT subscription handle — `drain()` returns events
    captured since the last drain (or since the session started).

    Used by the `midi_out_capture` pytest fixture. The fixture
    subscribes on setup, yields the session, drains-and-unsubscribes on
    teardown. Tests call `session.drain()` (or the shorter `session()`,
    which is equivalent) to assert on captured events mid-test.

    For non-pytest usage prefer `SchwungBus.capture_midi_out()` (a
    context manager).
    """
    def __init__(self, bus: "SchwungBus") -> None:
        self._bus = bus

    def drain(self) -> MidiOutCapture:
        return self._bus.dump_midi_out()

    def __call__(self) -> MidiOutCapture:
        return self.drain()


class MidiOutCaptureContext:
    """Context manager returned by SchwungBus.capture_midi_out().

    Subscribes on enter, drains-and-unsubscribes on exit. The captured
    events are exposed as `.events` (a MidiOutCapture) AFTER the
    `with` block.

    Use this for one-shot scripts and the API examples in the README.
    Inside pytest, prefer the `midi_out_capture` fixture which yields a
    `MidiOutSession` (no implicit __enter__/__exit__ confusion).
    """
    def __init__(self, bus: "SchwungBus") -> None:
        self._bus = bus
        self.events: MidiOutCapture = MidiOutCapture()

    def __enter__(self) -> "MidiOutCaptureContext":
        self._bus.subscribe_midi_out()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.events = self._bus.dump_midi_out()
        finally:
            try:
                self._bus.unsubscribe_midi_out()
            except Exception:
                pass
