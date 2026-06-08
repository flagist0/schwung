# Schwung OTLP tracing — design

**Date:** 2026-06-08
**Branch:** `feat/otlp-tracing` (our fork; not upstreamed)
**Status:** Phase 1 (shim/C) implemented + builds clean; pending device deploy +
gang review before enabling on the RT path. Phases 2–3 not started.

## Why

We keep hand-instrumenting `Date.now()` / `clock_gettime` to answer perf
questions (e.g. "ion's JS tick takes ~500 ms — where does it go?"). That
doesn't scale. We want a systematic, reusable tracing layer that emits
spans we can view in a normal trace UI (Grafana Tempo / Jaeger), with
parent/child structure and cross-process correlation.

## Hard constraints

1. **Zero overhead when off, and OFF BY DEFAULT.** Emission is a single
   predictable branch on an atomic flag when disabled.
2. **Realtime-safe emission.** The SPI callback runs SCHED_FIFO 90 on
   core 3 with a ~900 µs budget. The emission path must do NO malloc, NO
   file/network I/O, NO locks held by non-RT threads. It may only: read a
   monotonic clock and append a fixed-size record to a preallocated
   lock-free ring. Drop on full — never block.
3. **Export is off the hot path.** A dedicated exporter thread
   (SCHED_OTHER, never core 3) drains the ring, serializes, and writes
   out. All cost (serialization, I/O) lives there.
4. **No boilerplate at call sites.** One macro per scope; begin/end is
   implicit.

## Decisions (locked)

- **Macro-based, scoped.** `TRACE_SCOPE("name")` opens a span that
  auto-closes at end of the enclosing C block (via
  `__attribute__((cleanup))`). Hides all begin/end complexity. Manual
  `TRACE_BEGIN/END` available for spans that don't match a lexical scope.
- **Always compiled in, runtime-gated.** A `SCHWUNG_TRACE` compile flag
  can hard-compile-out to absolute zero, but the default build keeps it
  in and gates on a runtime atomic (`g_trace_on`), so we can enable on a
  shipped device via a touch-file without a rebuild.
- **File first, HTTP later.** The exporter writes OTLP/JSON to a rotating
  file on `/data/UserData/` (NOT `/tmp` — see device constraints). HTTP
  (`OTEL_EXPORTER_OTLP_ENDPOINT`, `:4318/v1/traces`) is a later swap of
  the same bytes via the vendored curl.
- **Order: shim → JS → ion.** Phase 1 = C shim. Phase 2 = JS bridge
  (`host_trace_span`). Phase 3 = ion's own spans.

## Architecture

```
emit (any thread, incl. RT):
  TRACE_SCOPE("spi.callback")
    -> if (!g_trace_on) no-op (one branch)
    -> else: push {name_id, t0, t1, tid, trace_id, span_id, parent_id}
             into a preallocated MPSC ring (atomic seq; drop on full)

exporter thread (SCHED_OTHER, off core 3):
  loop: drain ring -> group by trace -> build OTLP/JSON batch
        -> write JSONL line to /data/UserData/schwung/traces/<ts>.otlp.jsonl
        -> rotate at size cap; sleep when idle
```

### Span records (ring) — keep tiny + fixed

```c
typedef struct {
    uint32_t name_id;       /* index into interned name table */
    uint32_t tid;           /* OS thread id (gettid) */
    uint64_t t0_ns, t1_ns;  /* CLOCK_MONOTONIC_RAW */
    uint64_t trace_id;      /* per top-level scope (root) */
    uint64_t span_id;       /* monotone per-process counter */
    uint64_t parent_id;     /* 0 = root */
} trace_rec_t;              /* 48 bytes */
```

Ring: preallocated `trace_rec_t buf[TRACE_RING_CAP]` (power-of-two), a
single `_Atomic uint64_t write_seq`. Producers CAS-free: each takes a
slot via `atomic_fetch_add(write_seq, 1)` (MPSC), writes its record, done.
The exporter reads up to `write_seq`, tracking its own read cursor; if
producers lapped it (`write_seq - read > CAP`), it skips the gap and
counts drops (exported as a `trace.dropped` counter). No locks; tear-free
via the seq fence.

### Name interning

Span names are string literals known at compile time. `TRACE_SCOPE`
registers each literal once (first hit) into a static table and caches
the resulting `name_id` in a `static` local, so the hot path stores only
an int. The exporter resolves `name_id -> string` when serializing.

### Parent linkage + trace id

A per-thread span stack (small fixed array, thread-local). `begin`:
- if stack empty → this is a ROOT: mint a new `trace_id`, `parent_id=0`.
- else `parent_id = stack.top.span_id`, inherit `trace_id`.
- push; `end` pops and stamps `t1`, enqueues the record.

So `TRACE_SCOPE("spi.callback")` at the top of the SPI handler is the root
of one trace per frame; phase scopes inside become its children. Clean
flamegraph per frame.

### Enable / config

- Touch-file `/data/UserData/schwung/otlp_trace_on` → `g_trace_on = 1` +
  starts the exporter thread (lazy). Absent → off, thread idle/absent.
- Polled at init and on a slow timer (e.g. the existing 5 s telemetry
  tick), so it can be toggled live.
- Output dir `/data/UserData/schwung/traces/`. Rotate at e.g. 8 MB,
  keep N files.

### RT-safety invariants (emission)

- No allocation (ring + name table preallocated; thread stack is fixed).
- No locks (atomic seq only).
- No syscalls except `clock_gettime(CLOCK_MONOTONIC_RAW)` (vDSO) and
  `gettid` (cached per thread).
- Bounded work: O(1) per begin/end.
- Drop-on-full, never block.

### Cross-process (Phase 2+)

Schwung spans multiple processes (shim, `shadow_ui`, link-subscriber).
The ring lives in a **shared SHM segment** (`/schwung-trace`), so every
process — and the JS via a `host_trace_span(name, t0_ns, t1_ns)` C
binding — appends to the same ring, and ONE exporter thread (in the shim,
always alive) drains it. trace_ids correlate across processes. Phase 1
can start with a process-local ring and move to SHM when the JS bridge
lands.

## OTLP/JSON output

One `ExportTraceServiceRequest` per drained batch, newline-delimited
(JSONL) — replayable to a collector / importable to Tempo, and identical
to what the future HTTP mode POSTs.

```json
{"resourceSpans":[{"resource":{"attributes":[
  {"key":"service.name","value":{"stringValue":"schwung-shim"}}]},
  "scopeSpans":[{"scope":{"name":"shim"},"spans":[
    {"traceId":"<32hex>","spanId":"<16hex>","parentSpanId":"<16hex>",
     "name":"spi.callback","kind":1,
     "startTimeUnixNano":"...","endTimeUnixNano":"..."}]}]}]}
```

(Wall-clock offset: `clock_gettime(CLOCK_REALTIME)` once at exporter
start vs MONOTONIC_RAW, to convert span times to UnixNano.)

## Phase 1 scope (this branch) — DONE

- `src/host/schwung_trace.{h,c}` — macro API, ring, name table, exporter
  thread, OTLP/JSON file writer, enable via touch-file. **Exporter forced
  to SCHED_OTHER + pinned to cores 0-2** (it must not inherit the shim's
  FIFO-70 / land on core 3 — see `start_exporter()`).
- Wired into `src/schwung_shim.c`: `schwung_trace_init("schwung-shim")` in
  the SPI constructor; `schwung_trace_poll_enable()` on the 5 s timing
  logger thread; root spans `spi.pre` / `spi.post` at the top of each SPI
  callback; child spans `shadow.mix_audio`, `param.serve`, `midi.process`
  on the existing `[spi_timing]` boundaries. `param.serve` is the prime
  suspect for ion's slow tick.
- Added to `scripts/build.sh` (shim sources + deps). Builds clean (no new
  warnings).
- **Still TODO:** device deploy + gang review (RT-safety!) before enabling
  on the RT path.

### Using it (on device)

```bash
ssh ableton@move.local "touch /data/UserData/schwung/otlp_trace_on"   # on
# ... reproduce the slow scenario ...
ssh ableton@move.local "rm /data/UserData/schwung/otlp_trace_on"      # off
ls /data/UserData/schwung/traces/        # schwung-<ts>.otlp.jsonl
```

Each `.otlp.jsonl` line is one `ExportTraceServiceRequest`; replay to a
collector or import to Tempo/Jaeger. Off by default → zero hot-path cost.

## Later

- Phase 2: shared-SHM ring + `host_trace_span` JS binding → `shadow_ui`
  tick + per-`get_param` spans, correlated with the shim's param-serving
  span (answers "where does the 500 ms go" end-to-end).
- Phase 3: ion-internal spans (engine steps, emit pipeline).
- HTTP exporter (curl POST to OTLP/HTTP) as a config switch.
