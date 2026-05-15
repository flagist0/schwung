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

## Protocol v1

Line-based, ASCII, `\n`-terminated. One command per line, one response per
line. Replies start with `OK` or `ERR`.

| Request | Response |
| --- | --- |
| `PING` | `OK schwung-testd 0.1.0` |
| `INJECT_MIDI 0BB0307F` | `OK` |
| `WAIT_FRAME 5` | `OK frame=1234567` |
| `SNAPSHOT_PAD_LEDS` | `OK 00000000010000…` (64 hex chars = 32 bytes) |
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

## What this skeleton does NOT do yet

(All planned in later phases — see issue #2.)

* Streams (MIDI in/out, log, audio) with category masks
* Display framebuffer snapshots + syrupy diffing
* Module state providers (`host_register_test_state`)
* `device_files` fixture for SSH-backed file ops
* Combinator helpers (`bus.wait_all`)
* Sub-filters on streams (`midi_out:cable=0,status=note_off`)
* Audio fixture WAV-as-line-in injection
