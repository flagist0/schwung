/*
 * schwung-testd — test-bus daemon for on-device E2E tests.
 *
 * Listens on TCP loopback (default 127.0.0.1:47777), accepts a single client
 * connection at a time, and exposes a line-based text protocol that lets a
 * test runner inject MIDI events into Move's MIDI_IN buffer and snapshot
 * basic shim state (frame counter, pad LED colors).
 *
 * Talks to the shim purely through existing SHM contracts — no shim changes
 * required for the Phase 1 skeleton:
 *   /schwung-control      — read shim_counter for frame-sync ack
 *   /schwung-midi-inject  — write USB-MIDI packets into the inject ring
 *   /schwung-overlay      — read pad_led_colors snapshot
 *
 * Designed to be opt-in and dev-only: not started by the production
 * shim-entrypoint, no setuid, binds loopback by default. See README.md.
 *
 * Protocol v1 (line-based, \n-terminated, ASCII):
 *   PING                        -> OK schwung-testd <version>
 *   INJECT_MIDI <8-hex-chars>   -> OK            (1 USB-MIDI packet, 4 bytes)
 *   WAIT_FRAME <N>              -> OK frame=<counter>
 *   SNAPSHOT_PAD_LEDS           -> OK <64-hex-chars>
 *   QUIT                        -> OK bye        (server closes connection)
 *   <unknown>                   -> ERR <message>
 */

#define _GNU_SOURCE
#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#include "shadow_constants.h"
#include "shadow_midi_inject_writer.h"

#define TESTD_VERSION  "0.1.0"
#define TESTD_DEFAULT_PORT 47777
#define TESTD_LINE_MAX 256
#define TESTD_WAIT_FRAME_MAX 10000      /* hard cap to avoid hangs */
#define TESTD_WAIT_POLL_USEC 500        /* 0.5ms poll while waiting */
#define TESTD_WAIT_TIMEOUT_SEC 30       /* hard wall-clock cap */

/* --------------------------------------------------------------------------
 * Shared memory wiring
 * -------------------------------------------------------------------------- */

static shadow_control_t        *g_control  = NULL;
static shadow_midi_inject_t    *g_inject   = NULL;
static shadow_overlay_state_t  *g_overlay  = NULL;

static int map_shm_ro(const char *name, size_t size, void **out) {
    int fd = shm_open(name, O_RDONLY, 0666);
    if (fd < 0) {
        fprintf(stderr, "shm_open(%s, RO) failed: %s\n", name, strerror(errno));
        return -1;
    }
    void *p = mmap(NULL, size, PROT_READ, MAP_SHARED, fd, 0);
    close(fd);
    if (p == MAP_FAILED) {
        fprintf(stderr, "mmap(%s, RO) failed: %s\n", name, strerror(errno));
        return -1;
    }
    *out = p;
    return 0;
}

static int map_shm_rw(const char *name, size_t size, void **out) {
    int fd = shm_open(name, O_RDWR, 0666);
    if (fd < 0) {
        fprintf(stderr, "shm_open(%s, RW) failed: %s\n", name, strerror(errno));
        return -1;
    }
    void *p = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (p == MAP_FAILED) {
        fprintf(stderr, "mmap(%s, RW) failed: %s\n", name, strerror(errno));
        return -1;
    }
    *out = p;
    return 0;
}

static int wire_shm(void) {
    if (map_shm_ro(SHM_SHADOW_CONTROL, sizeof(shadow_control_t),
                   (void **)&g_control) < 0) return -1;
    if (map_shm_rw(SHM_SHADOW_MIDI_INJECT, sizeof(shadow_midi_inject_t),
                   (void **)&g_inject) < 0) return -1;
    if (map_shm_ro(SHM_SHADOW_OVERLAY, sizeof(shadow_overlay_state_t),
                   (void **)&g_overlay) < 0) return -1;
    return 0;
}

/* --------------------------------------------------------------------------
 * Hex helpers
 * -------------------------------------------------------------------------- */

static int hex_nibble(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return 10 + (c - 'a');
    if (c >= 'A' && c <= 'F') return 10 + (c - 'A');
    return -1;
}

/* Parse `len_chars` hex chars into `out` bytes (len_chars / 2 bytes).
 * Returns 0 on success, -1 on bad input. */
static int parse_hex(const char *s, size_t len_chars, uint8_t *out) {
    if (len_chars % 2 != 0) return -1;
    for (size_t i = 0; i < len_chars; i += 2) {
        int hi = hex_nibble(s[i]);
        int lo = hex_nibble(s[i + 1]);
        if (hi < 0 || lo < 0) return -1;
        out[i / 2] = (uint8_t)((hi << 4) | lo);
    }
    return 0;
}

static void format_hex(const uint8_t *bytes, size_t n, char *out) {
    static const char hex[] = "0123456789abcdef";
    for (size_t i = 0; i < n; i++) {
        out[i * 2]     = hex[(bytes[i] >> 4) & 0x0F];
        out[i * 2 + 1] = hex[bytes[i] & 0x0F];
    }
    out[n * 2] = '\0';
}

/* --------------------------------------------------------------------------
 * Command handlers — each writes its own response line via send_line()
 * -------------------------------------------------------------------------- */

static int send_line(int fd, const char *s) {
    size_t n = strlen(s);
    while (n > 0) {
        ssize_t w = send(fd, s, n, MSG_NOSIGNAL);
        if (w <= 0) {
            if (w < 0 && errno == EINTR) continue;
            return -1;
        }
        s += w;
        n -= (size_t)w;
    }
    return 0;
}

static int reply(int fd, const char *line) {
    char buf[TESTD_LINE_MAX + 2];
    snprintf(buf, sizeof(buf), "%s\n", line);
    return send_line(fd, buf);
}

static int reply_err(int fd, const char *msg) {
    char buf[TESTD_LINE_MAX];
    snprintf(buf, sizeof(buf), "ERR %s", msg);
    return reply(fd, buf);
}

static int cmd_ping(int fd, const char *args) {
    if (args && *args) return reply_err(fd, "PING takes no args");
    return reply(fd, "OK schwung-testd " TESTD_VERSION);
}

static int cmd_inject_midi(int fd, const char *args) {
    if (!args || strlen(args) != 8) {
        return reply_err(fd, "INJECT_MIDI expects 8 hex chars (1 USB-MIDI packet)");
    }
    uint8_t pkt[4];
    if (parse_hex(args, 8, pkt) < 0) {
        return reply_err(fd, "INJECT_MIDI: bad hex");
    }

    /* All four producers (shim, shadow_ui, shadow_chain forwarder, this
     * daemon) share /schwung-midi-inject. Coordination lives in the
     * MPSC helper — see src/host/shadow_midi_inject_writer.h. */
    int rc = shadow_midi_inject_push(g_inject, pkt);
    if (rc == -1) {
        return reply_err(fd, "INJECT_MIDI: inject buffer full, drain not running?");
    }
    if (rc == -2) {
        return reply_err(fd, "INJECT_MIDI: prior producer stranded, packet not committed");
    }
    return reply(fd, "OK");
}

static int cmd_wait_frame(int fd, const char *args) {
    if (!args) return reply_err(fd, "WAIT_FRAME expects N");
    char *end = NULL;
    long n = strtol(args, &end, 10);
    /* Require args to consume the entire token: `WAIT_FRAME 5junk` and
     * `WAIT_FRAME 0x10` (strtol stops at 'x') would otherwise be silently
     * accepted as 5 / 0. */
    if (end == args || *end != '\0' || n < 1 || n > TESTD_WAIT_FRAME_MAX) {
        return reply_err(fd, "WAIT_FRAME: N must be 1..10000");
    }
    uint32_t start = g_control->shim_counter;
    uint32_t target = start + (uint32_t)n;

    struct timespec t0, now;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    const long long timeout_ms = (long long)TESTD_WAIT_TIMEOUT_SEC * 1000LL;
    for (;;) {
        uint32_t cur = g_control->shim_counter;
        /* Signed delta handles uint32 wrap correctly. */
        if ((int32_t)(cur - target) >= 0) {
            char line[TESTD_LINE_MAX];
            snprintf(line, sizeof(line), "OK frame=%u", cur);
            return reply(fd, line);
        }
        clock_gettime(CLOCK_MONOTONIC, &now);
        long long elapsed_ms = (long long)(now.tv_sec - t0.tv_sec) * 1000LL
                             + (now.tv_nsec - t0.tv_nsec) / 1000000LL;
        if (elapsed_ms >= timeout_ms) {
            return reply_err(fd, "WAIT_FRAME: timeout (shim not ticking?)");
        }
        usleep(TESTD_WAIT_POLL_USEC);
    }
}

static int cmd_snapshot_pad_leds(int fd, const char *args) {
    if (args && *args) return reply_err(fd, "SNAPSHOT_PAD_LEDS takes no args");
    uint8_t copy[32];
    /* Volatile copy: shim writes asynchronously on the SPI thread. */
    for (int i = 0; i < 32; i++) {
        copy[i] = g_overlay->pad_led_colors[i];
    }
    char hex[65];
    format_hex(copy, 32, hex);
    char line[TESTD_LINE_MAX];
    snprintf(line, sizeof(line), "OK %s", hex);
    return reply(fd, line);
}

/* --------------------------------------------------------------------------
 * Per-connection request loop
 * -------------------------------------------------------------------------- */

static int read_line(int fd, char *out, size_t cap) {
    size_t n = 0;
    while (n < cap - 1) {
        char c;
        ssize_t r = recv(fd, &c, 1, 0);
        if (r == 0) return -1;             /* peer closed */
        if (r < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (c == '\r') continue;            /* tolerate CRLF */
        if (c == '\n') {
            out[n] = '\0';
            return (int)n;
        }
        if (c == '\0') return -1;           /* embedded NUL: lossy parse hazard */
        out[n++] = c;
    }
    return -1;                              /* line too long */
}

static int dispatch(int fd, char *line) {
    /* Split on first whitespace into <verb> + <args>. */
    char *args = NULL;
    for (char *p = line; *p; p++) {
        if (isspace((unsigned char)*p)) {
            *p = '\0';
            args = p + 1;
            while (*args && isspace((unsigned char)*args)) args++;
            break;
        }
    }
    /* Uppercase verb (case-insensitive) */
    for (char *p = line; *p; p++) *p = (char)toupper((unsigned char)*p);

    if (strcmp(line, "PING") == 0)              return cmd_ping(fd, args);
    if (strcmp(line, "INJECT_MIDI") == 0)       return cmd_inject_midi(fd, args);
    if (strcmp(line, "WAIT_FRAME") == 0)        return cmd_wait_frame(fd, args);
    if (strcmp(line, "SNAPSHOT_PAD_LEDS") == 0) return cmd_snapshot_pad_leds(fd, args);
    if (strcmp(line, "QUIT") == 0) {
        reply(fd, "OK bye");
        return 1;                                /* signal: close connection */
    }
    return reply_err(fd, "unknown command");
}

static void handle_client(int fd) {
    char line[TESTD_LINE_MAX];
    for (;;) {
        int n = read_line(fd, line, sizeof(line));
        if (n < 0) return;
        if (n == 0) continue;                    /* empty line, ignore */
        int rc = dispatch(fd, line);
        if (rc != 0) return;
    }
}

/* --------------------------------------------------------------------------
 * TCP listener
 * -------------------------------------------------------------------------- */

static int open_listener(const char *bind_addr, int port) {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) {
        perror("socket");
        return -1;
    }
    int one = 1;
    setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

    struct sockaddr_in sa = {0};
    sa.sin_family = AF_INET;
    sa.sin_port   = htons(port);
    if (inet_pton(AF_INET, bind_addr, &sa.sin_addr) != 1) {
        fprintf(stderr, "bad bind address: %s\n", bind_addr);
        close(s);
        return -1;
    }
    if (bind(s, (struct sockaddr *)&sa, sizeof(sa)) < 0) {
        fprintf(stderr, "bind(%s:%d) failed: %s\n", bind_addr, port, strerror(errno));
        close(s);
        return -1;
    }
    if (listen(s, 4) < 0) {
        perror("listen");
        close(s);
        return -1;
    }
    return s;
}

static volatile sig_atomic_t g_stop = 0;
static void on_signal(int sig) { (void)sig; g_stop = 1; }

int main(int argc, char **argv) {
    const char *bind_addr = getenv("SCHWUNG_TEST_BIND");
    if (!bind_addr) bind_addr = "127.0.0.1";
    const char *port_env = getenv("SCHWUNG_TEST_PORT");
    int port = port_env ? atoi(port_env) : TESTD_DEFAULT_PORT;
    if (port <= 0 || port > 65535) port = TESTD_DEFAULT_PORT;

    /* Allow --help / -h */
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) {
            printf("schwung-testd %s\n", TESTD_VERSION);
            printf("Usage: schwung-testd [--help]\n");
            printf("Env: SCHWUNG_TEST_BIND (default 127.0.0.1)\n");
            printf("     SCHWUNG_TEST_PORT (default %d)\n", TESTD_DEFAULT_PORT);
            return 0;
        }
    }

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    signal(SIGPIPE, SIG_IGN);

    if (wire_shm() < 0) {
        fprintf(stderr, "schwung-testd: failed to map SHM segments. "
                        "Is the shim running (MoveOriginal active)?\n");
        return 1;
    }

    int srv = open_listener(bind_addr, port);
    if (srv < 0) return 1;

    fprintf(stderr, "schwung-testd %s listening on %s:%d\n",
            TESTD_VERSION, bind_addr, port);

    while (!g_stop) {
        struct sockaddr_in ca;
        socklen_t cal = sizeof(ca);
        int c = accept(srv, (struct sockaddr *)&ca, &cal);
        if (c < 0) {
            if (errno == EINTR) continue;
            perror("accept");
            break;
        }
        char ip[INET_ADDRSTRLEN] = {0};
        inet_ntop(AF_INET, &ca.sin_addr, ip, sizeof(ip));
        fprintf(stderr, "schwung-testd: client connected from %s:%d\n",
                ip, ntohs(ca.sin_port));
        handle_client(c);
        close(c);
        fprintf(stderr, "schwung-testd: client disconnected\n");
    }

    close(srv);
    return 0;
}
