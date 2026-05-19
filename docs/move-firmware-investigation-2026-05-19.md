# Move firmware load analysis — 2026-05-19

> **Status:** ongoing. Last updated by an autonomous Claude session.
> Append new findings at the bottom; keep the executive summary at top
> in sync.

## TL;DR (start here)

1. **Root cause of Move's "always-hot" main thread = an O(N_sets)
   filesystem walker that fires ~460 times per second** with no
   inotify and no cache. Walker entry `FUN_01c6ec4c` in
   `MoveOriginal`, leaf `FUN_01c75b48` (getxattr wrapper). Triggered
   structurally by Browser/SongWheel view-model rebuilds.
2. **Empirically: 32,200 getxattr/sec on the test device** (14 sets),
   evenly distributed across 5 song-state xattr keys. Drops only 7%
   when ion takes over the UI. Constant ~25% of one core baseline.
3. **NOT the cause: Ableton Cloud sync** (disproved by RefreshToken
   A/B), the post-restart "spike" (it's the new steady state, not
   transient), or schwung's sync logging (batched version showed no
   variance reduction).
4. **The diagnostic tooling now lives in this repo** on branch
   `claude/distracted-curran-2f1183` (pushed to flagist0/schwung):
   - `src/host/sampling_profiler.{c,h}` — `perf_event_open(pid=0)`
     self-profiler in shim. Trigger:
     `touch /data/UserData/schwung/profile_on` + restart_move.
   - `src/host/xattr_counter.c` — passive LD_PRELOAD interposer
     counting getxattr/setxattr per second. Trigger:
     `touch /data/UserData/schwung/xattr_count_on`.
   - `tools/sampling_profiler/{parse_sprof,libc_lookup}.py` +
     `README.md` — offline symbolication + `--folded` mode for
     flamegraph.pl.
5. **Sketched-but-not-implemented mitigation**: shim-side LD_PRELOAD
   `getxattr`/`setxattr` cache keyed on (inode, key). Drops 25% to
   near-zero. Risk: external writers (Cloud sync daemon?) bypass
   the cache; needs `inotify(IN_ATTRIB)` invalidation thread for
   correctness. See Phase 6.6. **User approval needed before
   implementing.**
6. **Useful reference data on local disk**:
   - `/tmp/profile.bin` — most recent 25 s profile (11,574 samples)
   - `/tmp/profile_baseline.bin` — earlier identical-ish profile
   - `/tmp/profile.bin.symbols.txt` — libc symbol table for that
     profile (`<lib> <hex> <sym>` format, auto-found by parser)
   - `/tmp/profile.folded`, `/tmp/profile_baseline.folded` — pre-folded
     for flamegraph.pl
   - **`/tmp/move_flame.svg`** — interactive flame-graph rendered from
     `profile_baseline.folded`. Open in any browser. Each box is a
     stack frame; box width = sample count. The wide bottom band of
     `getxattr`/`fstatat`/`open`/`close` is the visualization of
     the 25% FS-walker cost.
   - `/tmp/flamegraph.pl` — downloaded helper script if you want to
     regenerate
   - `/tmp/xattr_phases.log` — capture spanning idle → ion-loaded
   - `/tmp/xattr_restart.log` — capture spanning restart_move
   - `/tmp/xattr_counter_baseline.log` — initial steady-state capture
     (30 s of ~32,200/sec readings)

## Executive summary

Move's `MoveOriginal` main thread (TID == PID, SCHED_OTHER) consumes
**~78% of one ARM core continuously** in steady state. The main thread
is not the audio realtime thread; that's `Audio Main/SPI` (~15% of one
core, FIFO scheduled).

The 78% breaks down approximately as:

| Source | % of profile samples (~25% of wall-time of core) | Subsystem |
|---|---|---|
| `getxattr` | 12.6% | filesystem walker (**EMPIRICALLY 32,200 calls/sec** — see Phase 6.55) |
| `fstatat`  | 5.5%  | filesystem walker |
| `open` + `close` | 5.3% | filesystem walker |
| `getdents64` | 2.8% | filesystem walker |
| `libXTCMalloc` ops | 3.5% | allocator (called from everywhere) |
| Flip JSON re-walk (`FUN_00e56b68`) | ~1% | CRDT model reconcile |
| Per-track DSP FX kernels (`0x01b5xxxx`) | small but recurring | legitimate audio work |
| `Model::tick` state-sync (`FUN_00828c14`) | ~1% | per-block reconcile |

Plus a long tail of generic libc++ / glibc plumbing called from the
hot paths.

**Cloud sync is NOT the cause.** Empirically disproved 2026-05-19 via
an A/B that moved `/data/UserData/settings/RefreshToken` aside,
restarted Move, and re-measured the main thread's jiffies/s — identical
to the with-token baseline (58 jiffies/s in both).

**The dominant cost is a synchronous filesystem polling loop** that
re-reads xattrs on every set in `/data/UserData/UserLibrary/Sets/<UUID>/`.
There are 5 xattrs per set, 14 sets on the current device → ~70
`getxattr` calls per scan. Profile sample rate vs scan rate suggests
the walker fires ~1 Hz, but the rate appears synchronous with
`Application::tick` which runs at audio rate (44 Hz).

**This is why our E2E tests have ~200 ms inject→LED variance** and why
`restart_move` always catches Move in a hot state: it's not a transient
spike, it's the steady state, and it scales with the number of sets in
the UserLibrary. The test fixture `pristine_set` v2 sidesteps the
problem at fixture-load time but doesn't change steady-state load.

---

## How we got here

### Phase 0: baseline

- ion's E2E suite has ~200 ms inject→LED median latency with stdev
  ~40 ms (log on) / 16 ms (log off). 41 tests × ~6 s restart_move
  overhead = ~4 min of pure restart per suite run. See
  `tests/e2e/TIMING.md` in the ion repo.
- Initial guess: Move's restart triggers a transient CPU spike that
  tests always catch. Tested with `SCHWUNG_POST_RESTART_SETTLE_S` env
  var in conftest.py — sleeps 0/15/30 s after `wait_for_shim_ready()`
  before `set_open_tool("ion")`. **Disproved**: stdev marginally
  tighter at settle=30 (~32 ms vs ~46 ms) but cost (30 s × 41 tests =
  20 min) far outweighs the gain. The min floor is identical (~125 ms)
  regardless of settle.

### Phase 1: where does Move's CPU actually go?

Linux capabilities on `/opt/move/MoveOriginal`
(`cap_ipc_lock,cap_sys_nice,cap_sys_resource=ep`) block:

- `ptrace` — so `strace`, `perf record -p`, `gdb -p` all fail with EPERM
- `/proc/<pid>/{maps,stack,syscall,mem}` — Permission denied
- `kptr_restrict=2` zeros out kernel pointers in `/proc/kallsyms`

ASLR is at `randomize_va_space=2` (full). No `sudo`. No root.

Fallback investigation used:
- `/proc/<pid>/task/<tid>/stat` per-thread CPU breakdown (readable)
- `/proc/<pid>/sched` for ctx switch counts
- `/proc/diskstats` system-wide I/O
- `/proc/loadavg` over time

Findings:
- 19 threads. Hot ones: main MoveOriginal (78%), Audio Main/SPI
  (15%), 3× Audio Worker (~8% each), plus Cloud Worker / Link Main /
  Resource Loader / D-Bus / sentry-http all in single digits.
- Loadavg climbs `2.0 → 3.9` over ~4 min from boot, then stays at
  3.5–4. **No transient spike** — that's the new steady state.
- Disk I/O is ~10 KB/s — CPU-bound, not I/O-bound.
- nr_voluntary_switches=13000 vs nr_involuntary_switches=246719 — the
  main thread is hitting its time slice (preempted) 95% of the time.

### Phase 2: log analysis

`/var/log/messages` rotates every ~5-10 minutes (200 KB each). The
dominant log line:

- `frames-dropped:N` — **2786 events in one 200 KB log file**, up to
  3248 in older ones. Move is constantly dropping audio frames.
- `audio-dropouts:1` — 4× per 15 min (less frequent, separate counter)
- `Memory Usage: 405 MB`, `Audio file cache size: 3.39 MB`, etc. —
  periodic 1-Hz health log, not a load source.

Ghidra-decompiled (via subagent) the log sites:
- `frames-dropped:N` printed by `FUN_0078c7e8` and `FUN_00741a98`. The
  `:N` is **per-tick delta**, not cumulative — a helper reads then
  zeroes the counter. Burst at startup = many ticks each dropping a
  few frames. There's no rate-limit / threshold to tune.
- `audio-dropouts:` printed by `FUN_007b1364` — different threshold
  semantics (gated on `engineTime() / sampleRate > threshold`).

**Startup burst origin**: at every `MoveOriginal` start the logs show:
```
Loading initial song
About to load /data/UserData/UserLibrary/Sets/<UUID>/BNYX Demo 2/Song.abl
Memory Usage: 154.68 MB    ← before
Memory Usage: 350.71 MB    ← +200 MB in 1 second
```
…followed by 100+ `frames-dropped:1` events in the next 3 seconds.
Every `restart_move` in tests pays this cost.

### Phase 3: how to skip the heavy demo load

Ghidra trace (via subagent):
- `FUN_009e4494` is the "Loading initial song" dispatcher
- `FUN_009cf198` parses `Settings.json`, matches the literal
  `"currentSongIndex"` (string at `0x00266950`), and sets a
  `has_value = (value != -1)` flag
- If the flag is false (or the path doesn't resolve), `FUN_009e4494`
  falls into `FUN_009e844c` = **`BuildDefaultSong`** — synthesizes an
  empty in-memory song with no sample loads. The C++ Application
  constructor's variant type literally has `BuildDefaultSong` as one of
  its tags.

**Actionable**: a test fixture can patch `Settings.json` to set
`currentSongIndex: -1`, restart_move, and Move boots in seconds with
no heavy load. **This already exists** as `pristine_set` v2 in
`tools/pytest-schwung/src/schwung_bus/pytest_plugin.py` (commits
`0ec672b8`, `0c3f9b24`, `0ebf32f4`). It uses a different approach (xattr
swap into a known template-set UUID) but the goal is the same.

### Phase 4: the sampling profiler

Discovered that capabilities don't block `perf_event_open(pid=0,
cpu=-1)` for self-profiling at `perf_event_paranoid=2`. Verified with a
20-line ARM64 program: got 819 samples in 200 ms of CPU burn.

Implemented `src/host/sampling_profiler.{h,c}` in the shim:

- Constructor opens `perf_event_open(pid=0)` with
  `PERF_SAMPLE_IP|TID|TIME|CALLCHAIN` at 1 kHz
- Reads `/proc/self/maps` once at init for offline symbolication
  (defeats ASLR — `/proc/self/*` is not blocked by caps)
- mmaps 256 KB ring buffer
- Background reader thread drains ring every 100 ms, writes binary
  records to `/data/UserData/schwung/profile.bin`
- Triggered by `/data/UserData/schwung/profile_on` flag file
- Self-stops when flag file is removed (no restart needed)

See `tools/sampling_profiler/README.md`.

First profile: 11574 samples in ~25 s on TID 1044 (main MoveOriginal).
Top hottest individual PCs:

| Count | Symbol |
|---|---|
| 1461 | `libc.so.6/getxattr+0xc` |
| 637  | `libc.so.6/fstatat+0x14` |
| 326  | `libc.so.6/getdents64+0x1c` |
| 324  | `libc.so.6/__open_nocancel+0x38` |
| 284  | `libc.so.6/__close_nocancel+0x10` |
| 172  | `libXTCMalloc.so+0x422c0` |
| 69   | `MoveOriginal+0xe57f34` (Flip JSON walker) |
| 53   | `MoveOriginal+0x61786c` |

The libc symbols were resolved by SCPing Move's `libc.so.6` to the
host and running `aarch64-linux-gnu-nm -D`. See
`tools/sampling_profiler/libc_lookup.py`.

### Phase 5: symbolication of MoveOriginal hot stacks (subagent)

Decompiled via Ghidra MCP. Key findings:

- **`FUN_00828c14`** (`0x00829398`) — `Model::tick` state-sync. Walks
  `mSong.tracks()`, rechecks lockId/lockSeal/selection, fires notifier
  chains. **Per-audio-block (44 Hz).**
- **`FUN_00e56b68`** (`0x00e57f34`) — Flip JSON document
  deserialization. Walks "name", "lockId", "lockSeal", "parameters",
  "deviceData", "chains" fields. 110 samples in one function. Looks
  like per-tick CRDT diff/snapshot.
- **`FUN_008dfd40`** — STL introsort on 5-qword structs with key at
  `[4]`. Resorts event lists every block.
- **`FUN_009454fc`** — `renameSong(uuid, name)` error path. The fact
  that this is hot suggests per-tick failing-rename retries or song
  lookups that miss.
- **`0x01b5xxxx` cluster** — initially classified as per-track DSP
  kernels, but they appear in FS-syscall callchains too. **More likely
  C++ template instantiations** (iterators / hash-table walkers) reused
  in both DSP and FS code. **A follow-up subagent is investigating
  the FS-walker call paths specifically.**
- `libXTCMalloc.so` — Allwinner/Xuantie tcmalloc fork; called from
  every `operator new`. ~3.5% of CPU is pure allocation overhead.

### Phase 6: xattr surface

Move stores 5 xattrs on each set directory:
```
user.last-modified-time="2026-05-19T17:44:03Z"
user.local-cloud-state="notSynced"   ← local marker, not actual cloud
user.song-color="3"
user.song-index="1"
user.was-externally-modified="false"
```

14 sets × 5 xattrs = 70 `getxattr` calls per full scan. At 58 samples/s
on `getxattr` (12.6% CPU on a 1 kHz sampler), that's roughly 58
calls/sec → ~1 Hz scan, OR 44 Hz scan × 1-2 attrs per tick.

The inotify infrastructure IS available on the device:
- `/proc/sys/fs/inotify/max_user_watches = 12537`
- `/proc/sys/fs/inotify/max_user_instances = 128`
- `/data` is ext4 (full inotify support including IN_ATTRIB for
  setxattr changes)

So polling is a **choice**, not a constraint. The next subagent will
investigate whether Move uses inotify at all, or polls because the
Cloud-sync daemon writes xattrs from a separate process whose changes
the Browser DBus service can't see directly.

### Phase 6.5: FS walker pinpointed (subagent #3 — confirmed no inotify)

Subagent #3 ("Find FS walker function + check inotify") concluded:

**Move uses NO inotify whatsoever.** Zero matches in the binary for
`inotify*`, `IN_CREATE`, `IN_MODIFY`, `IN_ATTRIB`. Only `epoll_*` /
`boost::asio` (network sockets). xattr changes DO fire `IN_ATTRIB` on
ext4, so this is a design choice, not a constraint.

The walker is **structurally on-demand** — fired by Browser/SongWheel
view-model rebuilds — but with no caching layer, every interaction that
dirties the browser model triggers a full O(N_sets) walk.

#### The actual call sites

- **`FUN_01c75b48`** at `0x01c75b48` — 6-line wrapper that calls libc
  `getxattr`. **All 41 getxattr profile samples collapse here**. This
  is the leaf.
- **`FUN_01c6ec4c`** at `0x01c6ec4c` — **the walker entry**. Iterates
  song descriptors; per set calls 5 xattr getters + 1 stat + several
  opens. The hot loop.
- Xattr-key-specific callers (all in
  `MoveFileManagementLib/src/SongFileManagement.cpp`):
  - `0x01c705ac` → reads `user.local-cloud-state` (string-matches
    `notSynced/synced/uploading/downloading/shouldSync/shouldUnsync/shouldDelete`)
  - `0x01c70c84` → reads `user.song-color`
  - `0x01c6dbbc` → reads `user.was-externally-modified`
  - `0x01c70f78` → writes `user.last-modified-time` as ISO-8601
  - `0x01c70d54` → encodes cloud-state for setxattr
  - `0x01c70b80` → 5-key WRITE (setxattr) backfill
- **Auto-assign variant `FUN_01c71838`** logs "Auto assign song
  attributes to <UUID>" / "Auto assigned in <ms>ms", called once with
  cap of `0x20=32` from a constructor at `0x009e3cf8`. So at boot,
  Move backfills missing xattrs on up to 32 sets.

#### Callers of `FUN_01c6ec4c` (what triggers the walk)

- `0x007be078` — iterates result, sets cloud-state=4 on items with
  state==1 (cloud-action loop)
- `0x01c6ea44` — lookup-by-name (Browser "find song")
- `0x01c701fc` — sum-of-indices
- `0x01c6ff38` — similar enumeration
- `0x009e4d44`, `0x009ef958`, `0x0096c018` — Browser/Wheel view model
  builders, virtually dispatched

#### `FUN_009fd208` revisited

Previously suspected as cloud poll, then as FS walker — turns out
**neither**. Subagent #3 confirms it's a one-shot
`moveCrashedOnLastStartup` xattr-delete, guarded by `param_1[1]==0`,
runs once at startup and never again. Subagent #1's classification was
wrong.

#### No config / no kill-switch

- No `FeatureFlags` entry near the walker call paths
- No `refresh_rate` / `poll_interval` constant
- No env var grep hit
- The walker is on-demand and unconditional

#### Why this melts on devices with many sets

The CPU cost scales linearly with `N_sets`. On the test device with
14 sets, the cost is ~12.6% (`getxattr` alone) of main thread CPU. On
a device with 80+ sets, this approaches the 25%+ that subagent's
extrapolation suggested.

The fix Ableton should ship: **a `BrowserState` cache invalidated on
the same code paths that already call `setxattr`** (they own all the
write sites). Or wire `inotify` on `/data/UserData/UserLibrary/Sets/`.
Either drops the cost to near-zero.

### Phase 6.55: EMPIRICAL call-rate measurement via xattr_counter (passive LD_PRELOAD)

To verify subagent #3's hypothesis directly, added `src/host/xattr_counter.c` —
a passive LD_PRELOAD interposer that wraps `getxattr`/`setxattr` (and the
`l*` variants), increments per-key atomic counters, and logs the
per-second delta from a background pthread. Gated by
`/data/UserData/schwung/xattr_count_on` flag file. **Always defers to
the real syscall — does NOT change behavior, just measures.**

**The measurement (Move idle, no ion loaded, no user interaction,
14 sets in UserLibrary, ~30 seconds of steady-state):**

```
20:02:36.698 xattr 1s: get=32200 set=0  [last-modified-time=g6440/s0 local-cloud-state=g6440/s0
                                          song-color=g6440/s0 song-index=g6440/s0
                                          was-externally-modified=g6440/s0 ]
20:02:37.699 xattr 1s: get=32200 ...same pattern...
20:02:38.699 xattr 1s: get=32122 ...
20:02:39.699 xattr 1s: get=32149 ...
20:02:40.699 xattr 1s: get=32189 ...
20:02:41.699 xattr 1s: get=32060 ...
```

**32,200 getxattr calls per second**, distributed evenly across the
5 song-state keys (~6,440 each), with `setxattr` count permanently 0.

Implications:
- 32200 / 14 sets / 5 keys = **460 full walks of the SongList per second**
- Each call costs ~3 μs (likely VFS-cached so kernel side is cheap),
  so 32200 × 3 μs = ~97 ms CPU/sec = ~10% wall-time on one core
- This matches the sampling profiler's ~12.6% getxattr fraction
  closely — the sampler was correct on CPU time, but vastly
  **underestimated** call rate because each call is sub-millisecond
  and rarely caught by the 1 kHz sampler
- 460 Hz doesn't match audio block rate (344 Hz). It's plausibly
  the rate at which Browser model dirty-checks happen, likely
  driven by multiple callers each rebuilding once per UI tick
- **Scaling implication for users with many sets**: 80 sets at this
  cadence would mean 184,000 getxattr/sec, costing ~550 ms CPU/sec
  on the same hardware — well over one full core, would absolutely
  saturate. **Move users with large libraries WILL see this as a
  performance bug.**

The counter has zero behavior impact (passes through unchanged), so
it's safe to leave in the shim build behind the flag. Useful as a
diagnostic on user devices to confirm the issue manifests there too.

**Cross-state comparison (Move IDLE vs ION LOADED):**

A second measurement loaded ion via testd's `set_open_tool("ion")`
mid-capture to see if entering overtake mode reduces FS-walker
pressure (since Move's UI is "hidden" under ion):

```
phase                  | get/sec (median)
IDLE (no ion)          | 32,200
TRANSITION (load ion)  | 30,000 (briefly)
STEADY (ion overtake)  | 29,800 (-7%)
```

**Move continues walking even when ion has taken over the UI.** The
Browser model dirty-checking still fires — just slightly less often
because overtake mode probably suppresses some screen-redraw triggers.
For our E2E tests this means the FS-walker noise is present in **all**
test scenarios, not just the brief transition during `set_open_tool`.
The xattr cache mitigation would benefit tests regardless of which
fixture/test class is active.

**Restart cycle behavior** (third measurement: 15s idle baseline →
restart_move via testd → 35s capture):

```
phase                 | get/sec
ion still loaded      | 29,800-30,900 (matches phase-2 above)
RESTART trigger (1s)  | 7,700 (MoveOriginal dying)
boot ramp (3-4s)      | 25,000-31,000 (variable)
post-boot idle        | 32,100-32,300 (rock-solid, no ion now)
```

The walker fires **steadily ~32,200/sec from t≈+5s post-restart**, no
extra burst from `Loading initial song` or `BNYX Demo 2/Song.abl`
deserialization (those costs are in different syscalls — `open` /
`read` on the big project file + samples, not xattr). So xattr-walker
load is **constant noise, not test-event-correlated**. This implies
test latency variance is caused by preemption windows / cross-process
sync chain effects, not directly by walker bursts.

The walker's ~460Hz cadence (32,200/sec ÷ 70 xattr-per-walk) doesn't
correspond to the audio block rate (344Hz). Plausibly tied to a UI
dirty-check timer or to multiple callers all rebuilding once per UI
tick. Could be confirmed by a Ghidra dive into the call sites at the
addresses subagent #3 listed (`0x009e4d44`, `0x009ef958`, `0x0096c018`).

### Phase 6.6: candidate shim mitigation (sketched, NOT implemented)

**LD_PRELOAD `getxattr` interposer with process-level cache.** The
shim already intercepts libc calls; adding `getxattr`/`setxattr` is
mechanically straightforward. Design:

```c
// In shim, with a map[ino_t][std::string key] -> std::string value
ssize_t getxattr(const char *path, const char *name, void *value, size_t size) {
    // restrict to the 5 well-known cached keys
    if (!is_song_state_key(name)) return real_getxattr(path, name, value, size);
    struct stat st;
    if (stat(path, &st) < 0) return real_getxattr(path, name, value, size);
    auto cached = cache_lookup(st.st_ino, name);
    if (cached) { memcpy(value, cached->data, cached->size); return cached->size; }
    ssize_t r = real_getxattr(path, name, value, size);
    if (r > 0) cache_store(st.st_ino, name, value, r);
    return r;
}
int setxattr(const char *path, const char *name, const void *value, size_t size, int flags) {
    int r = real_setxattr(path, name, value, size, flags);
    if (r == 0 && is_song_state_key(name)) {
        struct stat st;
        if (stat(path, &st) == 0) cache_store(st.st_ino, name, value, size);
    }
    return r;
}
```

**Risk**: if anything OUTSIDE MoveOriginal writes these xattrs (cloud
sync daemon? rsync from Move web UI?), the cache goes stale. Mitigation:
add an `inotify(IN_ATTRIB)` watch from a shim background thread on
`/data/UserData/UserLibrary/Sets/`, invalidate cache entries by inode
when an event fires. This is the **right** design but invasive
(~150 LoC + new thread + flag-gated rollout).

**Cheaper risk**: only mitigate WHILE TESTS ARE RUNNING. Triggered by
flag file `/data/UserData/schwung/xattr_cache_on`. Default off, no
behavior change for end users.

**Not implementing in this autonomous session** — needs user review of
design tradeoffs (especially the cloud sync daemon concern). Sketched
above for follow-up.

### Phase 7: what didn't work / disproved

- **batched-ring-buffer unified_log** (branch `feat/batched-unified-log`
  in `flagist0/schwung`): replaces sync `fprintf+fflush` with a
  background flusher. Built, deployed, validated. **No measurable
  variance reduction** in test conditions: stdev 48 ms (batched) vs
  41.5 ms (sync). At n=15 samples we can't distinguish noise. Design
  is sound but the multi-process sync chain dominates jitter, so logger
  improvements aren't visible. Branch parked on fork without upstream
  PR.
- **Cloud sign-out hypothesis**: subagent #1 strongly suggested
  `cloud_client::ICloudClientSync` poll inside `FUN_009fd208` as the
  CPU cost. A/B with `/data/UserData/settings/RefreshToken` renamed
  aside showed identical 58 jiffies/s. **`FUN_009fd208` is more likely
  the FS walker**, not cloud.

---

## What's actionable for our work

### Test infrastructure (ion E2E)

1. **`pristine_set` v2** already exists in pytest-schwung. ion's
   E2E tests don't use it yet; refactoring `ion_loaded` /
   `ion_with_test_project` to use it would (a) eliminate the
   BNYX Demo 2 load on every restart, (b) reduce restart_move wall-time
   from ~5 s to ~3 s, (c) eliminate the audio-dropout burst as a noise
   source.
2. **Reduce xattr surface** on the test device by keeping the
   UserLibrary minimal (1-2 template sets). Move's per-tick FS walker
   scales linearly with set count.
3. **Class-scoped fixtures** for tests that don't mutate state:
   `ion_loaded_class` + `ion_with_test_project_class` already added in
   ion's conftest.py (need test files refactored to opt in). Best case:
   41 restart_move → ~7–8, saves ~3.5 min/suite.
4. **`_filesystem_cleanup`** session fixture already added — wipes
   `MOVE_TEST_DIR/_test_*.json` and truncates `debug.log` between runs.

### Schwung shim improvements

1. **sampling_profiler.c** in shim is on this branch. Triggered by
   flag file, no effect when disabled. Could be merged once we trust
   it doesn't introduce realtime risk. (It runs in a non-RT pthread,
   on a non-RT main thread, no syscalls in hot SPI path — should be
   safe.)
2. **pthread_create hook** would extend profiler coverage to Audio
   threads. Open question: do we care about Audio Main/SPI (only 15%
   CPU vs main's 78%)?

### Useful only if Ableton picks them up

- **frames-dropped log rate-limit** — currently spams the log spammers
  spam. We can intercept via LD_PRELOAD if it becomes an issue but
  it's mostly cosmetic.
- **FS walker → inotify** — if the next subagent confirms it's pure
  polling, this is a worthwhile bug report.

---

## Open questions (to investigate when bandwidth allows)

1. ~~**Why does Move poll instead of inotify?**~~ Answered Phase 6.5:
   Move uses NO inotify at all. The walker is on-demand from Browser
   view-model rebuilds with no caching. Design choice, not constraint.
2. **Multi-thread profiling** — Audio Main/SPI at 15% CPU is also
   significant; is it pure DSP or is there room there too? Needs
   pthread_create hook in shim.
3. **Linear xattr scaling** — test by reducing set count to 1, profile,
   compare getxattr sample rate. Risky (touches user's actual sets),
   should ask first.
4. **Profile during E2E test** — does ion-loaded state change the
   ratio? Does running tests reduce or increase FS walker frequency?
5. **Disable shim baseline** — what does MoveOriginal alone look like?
   Probably similar (FS walker is in MoveLib, not the shim), but worth
   confirming.
6. **Implement the xattr cache shim hook** — sketched in Phase 6.6.
   Needs user review of cloud-sync-daemon staleness concern. Optional
   `inotify(IN_ATTRIB)` invalidation thread makes it bulletproof at
   the cost of complexity.
7. **Validate the `Auto assign song attributes` boot pass** — runs
   once at startup with cap 32 (`0x20`). On a device with 14 sets it
   probably runs once and completes. On a device with 80 sets it may
   need to run again. Check by searching debug log for "Auto assigned"
   timing.

---

## File index

Source code:
- `src/host/sampling_profiler.c` — the in-shim profiler
- `src/host/sampling_profiler.h` — public API
- `src/host/xattr_counter.c` — passive xattr call-rate counter
  (flag `xattr_count_on`)
- `scripts/build.sh` — extended source list to include profiler +
  counter

Tools:
- `tools/sampling_profiler/parse_sprof.py` — binary dump → top stacks
- `tools/sampling_profiler/libc_lookup.py` — libc offset → symbol
- `tools/sampling_profiler/README.md` — usage guide

Reference data on disk:
- `/tmp/profile_baseline.bin` — first successful capture (11574
  samples, TID 1044 main thread, debug_log_on=1, no ion loaded)
- `/tmp/move_libc.so.6` — copy of Move's libc for symbolication
- `/tmp/move_libc_syms.txt` — nm dump of Move's libc

ion-side changes:
- `tests/e2e/conftest.py` — added `_filesystem_cleanup` session
  fixture, `ion_loaded_class`, `ion_with_test_project_class`,
  `SCHWUNG_POST_RESTART_SETTLE_S` env knob
- `tests/e2e/TIMING.md` — documented settle-doesn't-help finding +
  batched-logger inconclusive validation

---

## Subagents used

1. **2026-05-19 ~19:18** — "Trace Move log strings via ghidra". Found
   `FUN_009e4494` (Loading initial song dispatcher), `FUN_009e844c`
   (BuildDefaultSong), `FUN_009cf198` (Settings.json parser),
   `FUN_009fd208` (originally suspected cloud poll, now suspect FS
   walker). Misclassified cloud_client as the CPU hog.
2. **2026-05-19 ~19:45** — "Symbolicate hot PCs in MoveOriginal".
   Decoded Model::tick, Flip JSON walker, STL sort, per-track DSP. Mis-
   classified `0x01b5xxxx` cluster as DSP only — likely shared
   template instantiations.
3. **2026-05-19 ~19:50** — "Find FS walker function + check inotify".
   **Definitive answer**: no inotify in MoveOriginal anywhere; walker
   is `FUN_01c6ec4c` (entry) → `FUN_01c75b48` (getxattr wrapper),
   triggered structurally by Browser/SongWheel view-model rebuilds
   with no caching. Suggested mitigation: shim-side LD_PRELOAD cache
   keyed on (inode, key) — see Phase 6.6.
