# pytest-schwung

Python client + pytest plugin for `schwung-testd`, the on-device test-bus
daemon. Use it to drive end-to-end tests against a real Move from your dev
machine: inject MIDI events, wait for SPI frames to elapse, snapshot pad LED
state.

This is the **Phase 1 skeleton**. Streams, display snapshots, audio, file
fixtures, and module state providers come in later phases — see
[flagist0/schwung#2](https://github.com/flagist0/schwung/issues/2).

## Architecture in one diagram

```
[ pytest on dev machine ]                  [ Move device ]
      │                                          │
      │  TCP localhost:47777                     │
      │  ◄──── SSH port-forward ──────►          │
      │                                          │
      └─► SchwungBus (this package)              │
              ├─ ping / inject_midi              │  schwung-testd
              ├─ wait_frame / snapshot_pad_leds  │  (C, opt-in)
              └─ press_pad / release_pad         │       │
                                                 │       ▼
                                                 │  /schwung-control     (RO)
                                                 │  /schwung-midi-inject (RW)
                                                 │  /schwung-overlay     (RO)
                                                 │       │
                                                 │       ▼
                                                 │  schwung-shim
                                                 │  (LD_PRELOAD'd into MoveOriginal)
```

The daemon talks to the live shim through the same SHM segments that
`shadow_ui` already uses — no shim modification is required for Phase 1.

## Quick start

### 1. Install the daemon on Move

The daemon is built as part of the standard schwung build and shipped in
the tarball at `bin/schwung-testd`:

```sh
./scripts/build.sh
./scripts/install.sh local --skip-modules --skip-confirmation
```

### 2. Start the daemon on Move

The daemon is **opt-in** and not started by `shim-entrypoint.sh`. Run it
manually over SSH:

```sh
ssh ableton@move.local /data/UserData/schwung/bin/schwung-testd
```

You should see:

```
schwung-testd 0.1.0 listening on 127.0.0.1:47777
```

Leave that SSH session open while you run tests.

### 3. Tunnel the port from your dev machine

In a second terminal:

```sh
ssh -L 47777:localhost:47777 ableton@move.local -N
```

Now `localhost:47777` on your dev machine reaches the daemon.

### 4. Install the plugin and run tests

```sh
pip install -e tools/pytest-schwung
pytest tests/e2e -v
```

If the daemon is unreachable, tests are skipped with a helpful message
rather than failing — safe to run in environments without a Move attached.

## Environment variables

| Var | Default | Effect |
| --- | --- | --- |
| `SCHWUNG_TEST_HOST` | `127.0.0.1` | Override target host (e.g. `move.local` to skip the SSH tunnel) |
| `SCHWUNG_TEST_PORT` | `47777` | Override target port |

The daemon honors `SCHWUNG_TEST_BIND` and `SCHWUNG_TEST_PORT` on its side.

## Direct API usage (no pytest)

```py
from schwung_bus import SchwungBus

with SchwungBus() as bus:
    print(bus.ping())                          # 'schwung-testd 0.1.0'
    before = bus.snapshot_pad_leds()
    bus.press_pad(84, velocity=100)
    bus.wait_frame(8)                          # block ~24ms
    after = bus.snapshot_pad_leds()
    bus.release_pad(84)
    print("changed indices:",
          [i for i in range(32) if before[i] != after[i]])
```

### Command pattern for reversible UI tests (Phase 3)

UI tests mutate Move's state. To stay isolated, each test should
undo what it did — preferably automatically. The `Commander` pattern
makes this declarative:

```py
from schwung_bus.move_commands import EnterTrackMenu, TapButton

def test_track_menu_does_something(bus, commander):
    commander.do(EnterTrackMenu())   # tap jog click
    commander.do(TapButton("menu"))  # do something in the menu
    # ... assertions ...
    # At test teardown, commander.undo_all() reverses everything LIFO:
    # first un-taps "menu", then taps "back" to close the track menu.
```

Each `Command` has:
- `precondition(bus, commander)` — raise `PreconditionError` if the
  system isn't in a valid state. Catches state drift loudly.
- `execute(bus, commander)` — apply the action.
- `undo(bus, commander)` — reverse it.

Preconditions consult `bus.state()` (a `BusState` snapshot of shim
state — `move_ui_mode`, `overtake_mode`, `shift_held`, `selected_slot`,
`ui_slot`, `shim_counter`, `transport_playing`). LED state is on
`bus.snapshot_pad_leds()` (32 bytes, notes 68-99) and
`bus.snapshot_step_leds()` (16 bytes, notes 16-31). When a test needs
finer granularity, extend the daemon's STATE response one field at a
time.

Concrete commands available today (in `schwung_bus.move_commands`):

- `EnterTrackMenu()` — tap jog; undo taps back
- `TapButton(name)` — generic tap with re-tap undo
- `SelectTrack(n, restore_to=1)` — tap CC 40..43 (reversed: CC43=Track1)
- `ToggleStep(n)` — press step pad note 16..31; undo re-presses

Preconditions and undo semantics are documented per-command. New
commands are added by subclassing `Command` (see `move_commands.py`
for templates).

The `commander` fixture handles teardown. Failures during `undo_all()`
raise `UndoError` and abort the test session — partial undo would
silently contaminate later tests.

### Capturing MIDI_OUT events (Phase 2)

The shim publishes every MIDI_OUT packet it observes to a SHM ring;
the daemon's `SUBSCRIBE_MIDI_OUT` / `DUMP_MIDI_OUT` / `UNSUBSCRIBE_MIDI_OUT`
expose it to tests. The Python client wraps it with a context manager
and a typed event class:

```py
with bus.capture_midi_out() as cap:
    bus.press_pad(84, velocity=100)
    bus.wait_frame(8)
    bus.release_pad(84)
    bus.wait_frame(20)

# After the with block, cap.events is a MidiOutCapture
note_ons  = cap.events.filter(kind="note_on", note=84).events
note_offs = cap.events.filter(kind="note_off", note=84).events
assert len(note_offs) >= len(note_ons), "stuck note!"
```

The pytest fixture `midi_out_capture` does the same wiring around a
test body — the fixture's teardown drains and unsubscribes even if the
test fails, so the next test starts with a clean baseline.

## Protocol v1

Line-based, ASCII, `\n`-terminated. One command per line, one response per
line. Replies start with `OK` or `ERR`.

| Request | Response |
| --- | --- |
| `PING` | `OK schwung-testd 0.1.0` |
| `INJECT_MIDI 0BB0307F` | `OK` |
| `WAIT_FRAME 5` | `OK frame=1234567` |
| `SNAPSHOT_PAD_LEDS` | `OK 00000000010000…` (64 hex chars = 32 bytes; notes 68-99) |
| `SNAPSHOT_STEP_LEDS` | `OK 00000000…` (32 hex chars = 16 bytes; notes 16-31) |
| `STATE` | `OK move_ui_mode=N overtake_mode=N shift_held=N selected_slot=N ui_slot=N shim_counter=N transport_playing=N` |
| `SUBSCRIBE <channel>` | `OK` (enables shim capture, sets baseline). v1 channels: `midi_out`. |
| `DUMP <channel>` | multi-line: `OK count=<N> dropped=<D>` then `EV <frame_hex> <pkt_hex>` × N, then `END` |
| `UNSUBSCRIBE <channel>` | `OK` (disables shim capture for that channel) |
| `QUIT` | `OK bye` (server then closes the connection) |

`INJECT_MIDI` takes one 4-byte USB-MIDI packet as 8 hex chars. Pad presses
are notes 68–99 on cable 0; the high nibble of byte 0 is the cable number,
the low nibble the CIN (`0x9` = note-on, `0x8` = note-off, `0xB` = CC).

`WAIT_FRAME N` blocks until the shim's SPI frame counter has advanced by at
least N (each frame ≈ 2.9 ms). Hard cap N ≤ 10000 and a 30 s wall-clock
ceiling guard against runaway tests.

`SNAPSHOT_PAD_LEDS` returns 32 bytes from `shadow_overlay_state_t.pad_led_colors`,
one per pad. Index 0 = note 68 (track 4 pad A), index 31 = note 99
(track 1 pad H).

## What this does NOT do yet

(All planned in later phases — see issue #2.)

* Streams for `midi_in`, `log`, `audio` (only `midi_out` so far)
* Display framebuffer snapshots + syrupy diffing
* Module state providers (`host_register_test_state`)
* `device_files` fixture for SSH-backed file ops (needed for the
  `pristine_set` fixture that combines a Settings.json `currentSongIndex`
  patch with `fresh_move` — would let tests start from a known empty
  set instead of "whatever song was last loaded")
* Combinator helpers (`bus.wait_all`)
* Server-side sub-filters on subscriptions (`midi_out:cable=0,status=note_off`) — for now, filter client-side via `cap.filter(...)`
* Audio fixture WAV-as-line-in injection

## Writing tests — pitfalls hard-won on hardware

The following are gotchas that bit while building the existing test suite.
They are listed once here rather than commented at every site that uses
them.

### Cable 0 carries MIDI *and* LED writes

Move emits pad-LED updates as `note_on` packets on cable 0 (the note
number identifies the pad, the velocity byte carries the color). A
"stuck note" test that matches `note_on(note=N)` on cable 0 will
trigger on every pad LED update for note N — including ones the test
itself caused. Real outgoing notes (to a downstream synth via USB) go
on **cable 2**. Filter to `cable=2` for stuck-note assertions, accept
that the test skips when no track is armed to USB MIDI OUT.

```py
cap = midi_out_capture.drain().filter(cable=2)
if len(cap) == 0:
    pytest.skip("no track armed to USB MIDI OUT")
```

### `move_ui_mode` and `selected_slot` mirror — covered for track CCs only

The shim's post-merge scan updates `move_ui_mode` and `selected_slot`
when a track CC (40-43) appears in the inject ring. **Other modifier
state — `shift_held` in particular — is hardware-only** (the shim's
shift handler runs additional debounce logic that the mirror skips).
If your test depends on `state.shift_held`, you must press shift on
the physical device; injecting CC 49 will inject but won't update the
mirror.

### Pad LED color drift is normal

Move shifts neighboring pads' brightness by ±1 byte during multi-pad
presses (e.g. a base color of `0x7B` can read as `0x7A` while another
pad is held). Exact-byte assertions on pad LED state are flaky for
this reason. Prefer:

* delta semantics (`after[i] != initial[i]` for indices we touched,
  unchanged for the rest)
* "not the bright press-glow color" rather than "equal to baseline"
* a settle window of 30 frames after release (Move holds the
  press-glow color for 15-25 frames before fading)

### Step LED colors: 0x5E base, 0x7A lit (per current set)

The faded ~0x5E base color shown on dim sequencer steps is NOT the
same as "step has no slot" (which would be 0). Don't treat
`step_leds[i] != 0` as "step is lit"; instead capture before/after
and look at the delta direction.

### Step toggling depends on note-edit mode

`ToggleStep` (and any step-pad injection) only flips the sequencer
state when Move is in NOTE view. With the shim state-mirror landed,
tapping any track button enters NOTE — see `SelectTrack(n)` for the
canonical setup step. Without that, step-pad injection does nothing
visible and the test silently passes a no-op.

### `bus` fixture is session-scoped, `commander` is per-test

`bus` opens one TCP connection at session start and keeps it open
across all tests — so the daemon sees ONE client throughout the run.
That client's per-connection state (current subscription, etc.) is
shared across tests, which is why `midi_out_capture` carefully
subscribes / unsubscribes around its body.

`commander` is per-test and auto-undoes on teardown. Use it for
any sequence of UI actions; the test body composes
`commander.do(Cmd())` calls and gets free reversal at the end.

### Long teardown chains can outlast SSH idle timeouts

Restart-move-style tests freeze the shim for ~3 seconds. Combined
with `commander.undo_all()`'s own waits, a single test can hold the
connection idle for tens of seconds. The SSH tunnel from the dev
machine needs `ServerAliveInterval=30` set or the kernel will idle
it out and the next test sees a stale socket. Use:

```
ssh -o ServerAliveInterval=30 -L 47777:localhost:47777 ableton@move.local -N
```
