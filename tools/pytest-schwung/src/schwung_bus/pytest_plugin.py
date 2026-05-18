"""Pytest entry point for pytest-schwung.

Fixtures:
  ``bus``               session-scoped SchwungBus, connected and
                        ping-validated. Auto-skips collected tests if
                        the daemon is unreachable.
  ``commander``         function-scoped Commander for UI tests with
                        auto-undo on teardown.
  ``fresh_move``        L2 reset: trigger restart-move.sh, wait for shim.
                        ~3 s. Same set, transient state cleared.
  ``pristine_set``      L2+ reset: overwrite Move's test-template Song.abl
                        with the repo's canonical empty version, then
                        restart-move. ~3 s plus an ssh cp (~30 ms over
                        the persistent ControlMaster). Use when tests
                        need a deterministic starting set instead of
                        whatever the user happened to leave loaded.
  ``midi_out_capture``  function-scoped MidiOutSession. The fixture
                        subscribes on setup, yields a session handle,
                        unsubscribes on teardown. Tests call
                        ``session.drain()`` to read captured events
                        (multiple times in one test is fine — each drain
                        returns events since the last).
"""

from __future__ import annotations

import shlex
import socket

import pytest

from .client import SchwungBus, SchwungBusError, MidiOutSession
from .commander import Commander
from .device_files import DeviceFiles, DeviceFilesError
from .pristine_constants import (
    DEVICE_STAGING_PATH,
    REPO_TEMPLATE_PATH,
    TEMPLATE_DEVICE_SONG_PATH,
    TEMPLATE_UUID,
)


def _do_restart(bus, timeout: int = 15) -> None:
    """Trigger restart-move and block until shim recovers.

    Shared by ``fresh_move`` and ``pristine_set`` so the protocol +
    timeout live in one place — when wait_for_shim_ready learns
    about a new freeze pattern, both fixtures get it.
    """
    bus.restart_move()
    bus.wait_for_shim_ready(timeout=timeout)


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
def fresh_move(bus):
    """Restart Move's firmware before this test (~3 s).

    Triggers ``restart-move.sh`` via the shim's restart_move flag —
    SIGTERMs+SIGKILLs the whole Move chain and relaunches fresh. Move
    reloads the same set on startup (currentSongIndex unchanged); this
    fixture resets transient state (active overtake, held modifiers,
    edit-mode position, etc.) but NOT the song content.

    Use this when:
      - your test needs Move's UI in a known reset state
      - you don't care which set is loaded (or you'll position via
        Commander after)

    For a fixture that ALSO swaps to a known empty template set, use
    ``pristine_set`` (TBD — needs device_files SSH helper).

    Skip ``fresh_move`` for fast in-set tests where Commander pattern
    undo suffices (3 s reset × N tests adds up in CI).
    """
    _do_restart(bus)
    yield


@pytest.fixture(scope="session")
def device_files():
    """Persistent SSH connection (ControlMaster) to the Move device.

    One open per session, reused by every fixture / test that needs
    file ops. Avoids ~200 ms SSH handshake on every call — after
    the first ``open()`` subsequent commands round-trip in ~30 ms.

    Skips collected tests if SSH itself is unreachable, so test
    runs on machines that can't ssh to the device degrade
    gracefully rather than fail with a cryptic stack trace.
    """
    dev = DeviceFiles()
    try:
        dev.open()
    except DeviceFilesError as e:
        pytest.skip(
            f"DeviceFiles SSH to {dev.host} unreachable ({e}). "
            "Ensure the device is up and SSH keys are configured."
        )
    yield dev
    dev.close()


@pytest.fixture(scope="session")
def _template_staged(device_files):
    """Stage the repo's canonical empty_song.abl onto Move once
    per session. Per-test ``pristine_set`` then does a local `cp`
    on the device (no network) to copy it into place.

    Verifies the template UUID dir actually exists on Move — if the
    user deleted or renamed _TEST_TEMPLATE without re-capturing the
    UUID in pristine_constants.py, this fixture fails fast with a
    clear message instead of letting per-test fixtures fail with
    ``cp: dest not found`` on every test.
    """
    if not REPO_TEMPLATE_PATH.is_file():
        pytest.skip(
            f"repo template missing: {REPO_TEMPLATE_PATH}. "
            "Re-capture from the device — see pristine_constants.py."
        )

    # Verify the destination dir exists on device. If not, the user
    # likely deleted/renamed the template — bail loudly.
    template_dir = (
        f"/data/UserData/UserLibrary/Sets/{TEMPLATE_UUID}"
    )
    if not device_files.file_exists(template_dir):
        pytest.skip(
            f"template UUID dir missing on device: {template_dir}. "
            "Either the template was deleted on Move, or "
            "TEMPLATE_UUID in pristine_constants.py is stale. "
            "Recreate _TEST_TEMPLATE on Move and re-capture the UUID."
        )

    device_files.put_file(REPO_TEMPLATE_PATH, DEVICE_STAGING_PATH)
    return DEVICE_STAGING_PATH


@pytest.fixture
def pristine_set(bus, device_files, _template_staged):
    """Reset Move to the canonical empty test template before this test.

    Per test:
      1. ssh cp staging-path → template Song.abl (local on device, ~30 ms)
      2. restart_move() → shim picks up the flag, kills + relaunches Move
      3. wait_for_shim_ready() → blocks through the freeze + thaw cycle

    Total cost: ~3 s (dominated by the restart-move cycle; the cp itself
    is negligible).

    Scope coupling: function-scoped, depends on ``bus`` (session),
    ``device_files`` (session), ``_template_staged`` (session). If
    ``bus`` is ever narrowed to function scope, the session-scoped
    SSH connection in ``device_files`` keeps working — but be careful
    that ``_template_staged`` still runs before the first per-test
    use of ``pristine_set``.

    No teardown — the next test that needs a pristine set will
    overwrite the file again anyway. Tests that want strict cleanup
    should also use Commander and rely on its undo stack. If
    ``restart_move()`` raises mid-fixture (network hiccup, daemon
    crash), the device is left with the template Song.abl already in
    place; the next pristine_set re-`cp`s and re-restarts, so the
    contamination is at worst one test's ordering artifact, not a
    hard break.

    Use this when your test would otherwise depend on whatever pattern
    / instruments the user happened to leave loaded — e.g. step LED
    tests that need known dim baseline, pad LED tests that need
    notes-off starting state. For fast tests where Commander undo
    suffices, prefer Commander; pristine_set adds ~3 s per test.
    """
    device_files.run(
        f"cp {DEVICE_STAGING_PATH} {shlex.quote(TEMPLATE_DEVICE_SONG_PATH)}"
    )
    _do_restart(bus)
    yield


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
