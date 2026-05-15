"""TCP client for schwung-testd, the on-device test-bus daemon.

Wraps the v1 line protocol (PING / INJECT_MIDI / WAIT_FRAME / SNAPSHOT_PAD_LEDS
/ QUIT) and exposes both raw primitives and semantic helpers (press_pad,
release_pad). The daemon listens on TCP loopback by default; reach it from a
dev machine via `ssh -L 47777:localhost:47777`.

Sequential, single-connection — matches the Phase 1 daemon, which accepts
one client at a time. Threading and async are out of scope for v1.
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass
from typing import Optional

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 47777


class SchwungBusError(RuntimeError):
    """Raised when the daemon returns an ERR response or the protocol breaks."""


@dataclass
class WaitFrameResult:
    counter: int


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
