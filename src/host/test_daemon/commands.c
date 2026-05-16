/*
 * commands.c — verb table + handlers for schwung-testd.
 *
 * One verb-table row per supported command; handlers operate on the
 * SHM pointers wired in via commands_init(). Adding a Phase 2 command
 * (e.g. SUBSCRIBE, DUMP) means adding a handler here and listing it in
 * the table — no other file changes.
 */

#define _GNU_SOURCE

#include "commands.h"
#include "protocol.h"
#include "shadow_midi_inject_writer.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define TESTD_VERSION  "0.1.0"

/* WAIT_FRAME guards: hard cap on N (avoid runaway tests) and on wall
 * clock (avoid hangs if the shim stops ticking). */
#define TESTD_WAIT_FRAME_MAX     10000
#define TESTD_WAIT_POLL_USEC     500
#define TESTD_WAIT_TIMEOUT_SEC   30

/* SHM pointers, set by commands_init() and held until process exit. */
static daemon_shm_t g_shm;

void commands_init(const daemon_shm_t *shm) {
    g_shm = *shm;
}

/* Forward decls so commands_reset_client_state can clear stream state
 * before the per-stream globals are defined below. */
static void midi_out_subscription_reset(void);

void commands_reset_client_state(void) {
    /* Called from schwung_testd.c after a client disconnects. Disables
     * any subscriptions the prior client opened so the next client
     * starts from a clean slate — without this, a dropped TCP
     * connection (network blip, killed test runner, exception that
     * bypassed QUIT) leaves the shim publishing into a ring we'd then
     * misinterpret as "events captured for the new client." */
    midi_out_subscription_reset();
}

/* ---- handlers ---------------------------------------------------------- */

static int cmd_ping(int fd, const char *args) {
    if (args && *args) return protocol_reply_err(fd, "PING takes no args");
    return protocol_reply(fd, "OK schwung-testd " TESTD_VERSION);
}

static int cmd_inject_midi(int fd, const char *args) {
    if (!args || strlen(args) != 8) {
        return protocol_reply_err(fd, "INJECT_MIDI expects 8 hex chars (1 USB-MIDI packet)");
    }
    uint8_t pkt[4];
    if (protocol_parse_hex(args, 8, pkt) < 0) {
        return protocol_reply_err(fd, "INJECT_MIDI: bad hex");
    }

    /* All four producers (shim, shadow_ui, shadow_chain forwarder, this
     * daemon) share /schwung-midi-inject. Coordination lives in the
     * MPSC helper — see src/host/shadow_midi_inject_writer.h. */
    int rc = shadow_midi_inject_push(g_shm.inject, pkt);
    if (rc == -1) {
        return protocol_reply_err(fd, "INJECT_MIDI: inject buffer full, drain not running?");
    }
    if (rc == -2) {
        return protocol_reply_err(fd, "INJECT_MIDI: prior producer stranded, packet not committed");
    }
    return protocol_reply(fd, "OK");
}

static int cmd_wait_frame(int fd, const char *args) {
    if (!args) return protocol_reply_err(fd, "WAIT_FRAME expects N");
    char *end = NULL;
    long n = strtol(args, &end, 10);
    /* Require args to consume the entire token: `WAIT_FRAME 5junk` and
     * `WAIT_FRAME 0x10` (strtol stops at 'x') would otherwise be silently
     * accepted as 5 / 0. */
    if (end == args || *end != '\0' || n < 1 || n > TESTD_WAIT_FRAME_MAX) {
        return protocol_reply_err(fd, "WAIT_FRAME: N must be 1..10000");
    }
    uint32_t start = g_shm.control->shim_counter;
    uint32_t target = start + (uint32_t)n;

    struct timespec t0, now;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    const long long timeout_ms = (long long)TESTD_WAIT_TIMEOUT_SEC * 1000LL;
    for (;;) {
        uint32_t cur = g_shm.control->shim_counter;
        /* Signed delta handles uint32 wrap correctly. */
        if ((int32_t)(cur - target) >= 0) {
            char line[TESTD_LINE_MAX];
            snprintf(line, sizeof(line), "OK frame=%u", cur);
            return protocol_reply(fd, line);
        }
        clock_gettime(CLOCK_MONOTONIC, &now);
        long long elapsed_ms = (long long)(now.tv_sec - t0.tv_sec) * 1000LL
                             + (now.tv_nsec - t0.tv_nsec) / 1000000LL;
        if (elapsed_ms >= timeout_ms) {
            return protocol_reply_err(fd, "WAIT_FRAME: timeout (shim not ticking?)");
        }
        usleep(TESTD_WAIT_POLL_USEC);
    }
}

static int cmd_snapshot_pad_leds(int fd, const char *args) {
    if (args && *args) return protocol_reply_err(fd, "SNAPSHOT_PAD_LEDS takes no args");
    uint8_t copy[32];
    /* Volatile copy: shim writes asynchronously on the SPI thread. */
    for (int i = 0; i < 32; i++) {
        copy[i] = g_shm.overlay->pad_led_colors[i];
    }
    char hex[65];
    protocol_format_hex(copy, 32, hex);
    char line[TESTD_LINE_MAX];
    snprintf(line, sizeof(line), "OK %s", hex);
    return protocol_reply(fd, line);
}

/* ---- Phase 2: MIDI_OUT stream subscription ---------------------------- */

/* Per-subscriber baseline: the write_seq value at the moment of
 * SUBSCRIBE_MIDI_OUT (or the most recent DUMP_MIDI_OUT). Events after
 * this point haven't been delivered yet. Single global because the
 * daemon serves one client at a time; commands_reset_client_state()
 * clears it on disconnect so the next client doesn't inherit stale
 * baselines. */
static uint32_t g_midi_out_baseline = 0;
static int      g_midi_out_subscribed = 0;

static void midi_out_subscription_reset(void) {
    if (g_shm.midi_out_stream) {
        __atomic_store_n(&g_shm.midi_out_stream->enabled, 0, __ATOMIC_RELEASE);
    }
    g_midi_out_subscribed = 0;
    g_midi_out_baseline = 0;
}

static int cmd_subscribe_midi_out(int fd, const char *args) {
    if (args && *args) return protocol_reply_err(fd, "SUBSCRIBE_MIDI_OUT takes no args");
    test_stream_shm_t *s = g_shm.midi_out_stream;
    if (!s) return protocol_reply_err(fd, "test-stream SHM not mapped");

    /* Enable capture in the shim and snapshot the current write_seq so
     * subsequent DUMP returns only events captured from this point on.
     * If already subscribed, this acts as a reset. */
    __atomic_store_n(&s->enabled, 1, __ATOMIC_RELEASE);
    g_midi_out_baseline = __atomic_load_n(&s->write_seq, __ATOMIC_ACQUIRE);
    g_midi_out_subscribed = 1;
    return protocol_reply(fd, "OK");
}

static int cmd_unsubscribe_midi_out(int fd, const char *args) {
    if (args && *args) return protocol_reply_err(fd, "UNSUBSCRIBE_MIDI_OUT takes no args");
    test_stream_shm_t *s = g_shm.midi_out_stream;
    if (s) __atomic_store_n(&s->enabled, 0, __ATOMIC_RELEASE);
    g_midi_out_subscribed = 0;
    return protocol_reply(fd, "OK");
}

/* DUMP_MIDI_OUT response format (multi-line):
 *   OK count=<N> dropped=<D>
 *   EV <frame_hex> <pkt_hex>     (×N lines)
 *   END
 * frame_hex is 8 hex chars (uint32). pkt_hex is 8 hex chars (4 USB-MIDI
 * bytes). Lines kept short and parser-friendly. */
static int cmd_dump_midi_out(int fd, const char *args) {
    if (args && *args) return protocol_reply_err(fd, "DUMP_MIDI_OUT takes no args");
    test_stream_shm_t *s = g_shm.midi_out_stream;
    if (!s) return protocol_reply_err(fd, "test-stream SHM not mapped");
    if (!g_midi_out_subscribed) {
        return protocol_reply_err(fd, "not subscribed (call SUBSCRIBE_MIDI_OUT first)");
    }

    /* ACQUIRE pairs with shim's RELEASE on write_seq → buffer writes
     * are visible to us. */
    uint32_t cur = __atomic_load_n(&s->write_seq, __ATOMIC_ACQUIRE);

    /* Detect shim restart / SHM reset: if cur is BEFORE our baseline
     * (signed delta negative), the underlying write_seq was zeroed
     * while we held an old value. Treat as a fresh start: re-baseline
     * silently and return empty. Otherwise the unsigned subtraction
     * below would wrap to ~4 billion and we'd report nonsense
     * `dropped` plus 1024 garbage events from the buffer. */
    if ((int32_t)(cur - g_midi_out_baseline) < 0) {
        g_midi_out_baseline = cur;
        if (protocol_reply(fd, "OK count=0 dropped=0") < 0) return -1;
        return protocol_reply(fd, "END");
    }

    uint32_t delta = cur - g_midi_out_baseline;
    uint32_t to_read = delta;
    uint32_t dropped = 0;
    uint32_t first = g_midi_out_baseline;

    if (delta > TEST_STREAM_CAPACITY) {
        /* Writer wrapped past us; oldest events were overwritten. */
        dropped = delta - TEST_STREAM_CAPACITY;
        to_read = TEST_STREAM_CAPACITY;
        first = cur - TEST_STREAM_CAPACITY;
    }

    char hdr[TESTD_LINE_MAX];
    snprintf(hdr, sizeof(hdr), "OK count=%u dropped=%u", to_read, dropped);
    if (protocol_reply(fd, hdr) < 0) return -1;

    /* Copy each event out (snapshot per-event so the buffer slot can be
     * overwritten by the shim mid-dump without corrupting our read). */
    for (uint32_t i = 0; i < to_read; i++) {
        uint32_t seq = first + i;
        test_stream_event_t ev = s->buffer[seq % TEST_STREAM_CAPACITY];
        char pkt_hex[9];
        protocol_format_hex(ev.pkt, 4, pkt_hex);
        char line[TESTD_LINE_MAX];
        snprintf(line, sizeof(line), "EV %08x %s", ev.frame, pkt_hex);
        if (protocol_reply(fd, line) < 0) return -1;
    }

    if (protocol_reply(fd, "END") < 0) return -1;
    g_midi_out_baseline = cur;
    return 0;
}

static int cmd_quit(int fd, const char *args) {
    (void)args;
    /* Symmetric with the post-disconnect cleanup in schwung_testd.c —
     * either path leaves the shim with no live subscription. */
    midi_out_subscription_reset();
    protocol_reply(fd, "OK bye");
    return 1;  /* signal: close connection after this reply */
}

/* ---- dispatch ---------------------------------------------------------- */

typedef int (*command_fn)(int fd, const char *args);

typedef struct {
    const char *name;
    command_fn  handler;
} command_entry_t;

static const command_entry_t g_commands[] = {
    {"PING",                 cmd_ping},
    {"INJECT_MIDI",          cmd_inject_midi},
    {"WAIT_FRAME",           cmd_wait_frame},
    {"SNAPSHOT_PAD_LEDS",    cmd_snapshot_pad_leds},
    {"SUBSCRIBE_MIDI_OUT",   cmd_subscribe_midi_out},
    {"UNSUBSCRIBE_MIDI_OUT", cmd_unsubscribe_midi_out},
    {"DUMP_MIDI_OUT",        cmd_dump_midi_out},
    {"QUIT",                 cmd_quit},
    {NULL, NULL},
};

int commands_dispatch(int fd, const char *verb, const char *args) {
    for (const command_entry_t *c = g_commands; c->name; c++) {
        if (strcmp(verb, c->name) == 0) {
            return c->handler(fd, args);
        }
    }
    return protocol_reply_err(fd, "unknown command");
}
