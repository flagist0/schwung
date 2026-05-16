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

static int cmd_quit(int fd, const char *args) {
    (void)args;
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
    {"PING",              cmd_ping},
    {"INJECT_MIDI",       cmd_inject_midi},
    {"WAIT_FRAME",        cmd_wait_frame},
    {"SNAPSHOT_PAD_LEDS", cmd_snapshot_pad_leds},
    {"QUIT",              cmd_quit},
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
