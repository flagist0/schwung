# sampling_profiler

In-process sampling profiler for the schwung shim. Captures instruction
pointers + call chains of MoveOriginal's threads without needing root,
ptrace, or kernel-mode perf — works because the shim runs inside Move
via LD_PRELOAD and `perf_event_open(pid=0, cpu=-1)` allows self-profiling
even at the Move kernel's default `perf_event_paranoid=2`.

## Why this exists

Move's `MoveOriginal` binary has Linux capabilities baked in
(`cap_ipc_lock,cap_sys_nice,cap_sys_resource=ep`) which cause the kernel
to refuse `ptrace`, deny `/proc/<pid>/{maps,stack,syscall,mem}` reads,
and (combined with `kptr_restrict=2`) zero out kernel pointers in
`/proc/kallsyms`. `strace -p <pid>`, `perf record -p <pid>`, `gdb -p
<pid>` all return EPERM. We have no `sudo` and the device has no root.

But: those caps protect OUTSIDE → INSIDE access. Same-process
self-introspection is not blocked. The shim runs inside MoveOriginal via
LD_PRELOAD, so `perf_event_open(pid=0)` and `/proc/self/maps` both
succeed.

## Components

- `../src/host/sampling_profiler.{h,c}` — in-shim profiler. Runs when
  `/data/UserData/schwung/profile_on` is present at shim load OR
  appears later (reader thread checks every ~1s).
- `parse_sprof.py` — host-side parser. Reads the binary dump, walks
  the embedded `/proc/self/maps`, prints the top hot PCs (by sample
  count) and the top folded stacks (leaf → root, `;` separated).
- `libc_lookup.py` — symbolicates libc offsets against a host-side
  `nm` extraction (one-time setup; see below).

## Usage

### Enable profiling

```bash
ssh ableton@move.local "touch /data/UserData/schwung/profile_on"
ssh ableton@move.local "/data/UserData/schwung/restart-move.sh"
# wait a bit, file fills at ~50 KB/sec
ssh ableton@move.local "ls -la /data/UserData/schwung/profile.bin"
```

The profiler attaches to the thread that loads the shim, which under
LD_PRELOAD is the main MoveOriginal thread (TID == PID, the one burning
~78% of one core). Other threads aren't sampled yet — a pthread_create
hook would extend coverage to Audio Main/SPI etc.

### Capture & symbolicate

```bash
ssh ableton@move.local "rm /data/UserData/schwung/profile_on"
# wait ~2s for reader thread to flush
scp ableton@move.local:/data/UserData/schwung/profile.bin /tmp/
python3 tools/sampling_profiler/parse_sprof.py /tmp/profile.bin 20
```

### Symbolicate libc offsets

The hot list shows entries like `libc.so.6+0xe768c` — to resolve those
to symbol names you need Move's libc:

```bash
scp ableton@move.local:/lib/libc.so.6 /tmp/move_libc.so.6
docker run --rm -v /tmp:/work schwung-builder \
    aarch64-linux-gnu-nm -D --defined-only /work/move_libc.so.6 \
    | awk '{print $1, $3}' > /tmp/move_libc_syms.txt
python3 tools/sampling_profiler/libc_lookup.py
```

(Edit `libc_lookup.py` to point at the offsets you care about.)

### Symbolicate MoveOriginal offsets

MoveOriginal is a PIE binary loaded into Ghidra (project at
`/Users/alex/tmp/move_re/move_re.gpr`). Ghidra addresses == file offsets
directly (image base = 0), so the profile's `MoveOriginal+0xN` maps to
Ghidra address `0x000N` literally. Open the address in Ghidra and
decompile.

For batches: spawn a Claude Code subagent with the offsets and ask it
to use the Ghidra MCP server (`mcp__ghidra__decompile_function_by_address`).
See ghidra-subagent prompts in
`docs/move-firmware-investigation-2026-05-19.md` for examples.

## Binary format

Little-endian. See `parse_sprof.py` for the canonical reader.

```
magic[8]    = "SPROF\0\0\0"
uint32 ver  = 1
uint32 hz   = 1000
uint64 t0   = clock_gettime(CLOCK_MONOTONIC) at start
uint32 maps_len
char   maps[maps_len]    (verbatim /proc/self/maps content)

--- repeating records ---
uint8 tag
  == 'S' (sample):
    uint64 ip
    uint32 tid
    uint64 time_ns
    uint16 nr_pc
    uint64 pc[nr_pc]    (callchain, innermost first)
  == 'E' (end marker)
```

## Limitations

- Only profiles the thread that loads the shim (main thread). Add a
  pthread_create wrapper to extend to other threads.
- Ring buffer is 256 KB; at very high event rates samples can be lost
  (counted as `samples_lost` in the shutdown log line).
- Symbolication for stripped libraries gives `lib+0xN` only — install
  debug symbols separately for libc++ and libXTCMalloc if needed.
- The constructor in sampling_profiler.c is racy with other LD_PRELOAD
  constructors that fork threads early. Tested fine with the current
  shim's startup order.
