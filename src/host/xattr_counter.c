/*
 * xattr_counter.c — passive LD_PRELOAD interposer that counts
 * Move's getxattr/setxattr/lgetxattr calls without changing any
 * behavior. Periodically logs the rate so we can correlate Move's
 * FS-walker activity with user actions.
 *
 * Gate: presence of /data/UserData/schwung/xattr_count_on at shim
 * load (and re-checked once per second). No effect when disabled.
 *
 * This is the cheap empirical complement to the sampling profiler:
 * the profiler tells us "12.6% of CPU is in getxattr"; this tells
 * us exact CALL RATES, broken down by (xattr key, path-pattern).
 * If the rate matches Browser interactions, we've confirmed
 * subagent #3's hypothesis directly.
 *
 * Why not just use the profiler? The profiler counts ON-CPU
 * samples, which understates calls that finish in <1ms. This
 * gives us TRUE call counts.
 */

#include "unified_log.h"

#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/xattr.h>
#include <time.h>
#include <unistd.h>

#define XATTR_COUNT_FLAG_PATH "/data/UserData/schwung/xattr_count_on"

/* The 5 song-state keys subagent #3 identified, plus an "other" bucket. */
enum { K_LAST_MODIFIED, K_CLOUD_STATE, K_SONG_COLOR, K_SONG_INDEX,
       K_EXTERNALLY_MODIFIED, K_OTHER, N_KEY_BUCKETS };

static const char *KEY_NAMES[N_KEY_BUCKETS] = {
    "user.last-modified-time",
    "user.local-cloud-state",
    "user.song-color",
    "user.song-index",
    "user.was-externally-modified",
    "<other>",
};

/* Live counters, incremented from any thread (including audio).
 * Atomic to avoid races with the reporter thread. */
static atomic_uint_least64_t g_get_count[N_KEY_BUCKETS];
static atomic_uint_least64_t g_set_count[N_KEY_BUCKETS];

/* Real libc functions, resolved lazily via dlsym. */
static ssize_t (*real_getxattr)(const char *, const char *, void *, size_t) = NULL;
static ssize_t (*real_lgetxattr)(const char *, const char *, void *, size_t) = NULL;
static int (*real_setxattr)(const char *, const char *, const void *, size_t, int) = NULL;
static int (*real_lsetxattr)(const char *, const char *, const void *, size_t, int) = NULL;
static pthread_once_t g_dlsym_once = PTHREAD_ONCE_INIT;

static volatile int g_enabled = 0;

static void resolve_real(void) {
    real_getxattr = dlsym(RTLD_NEXT, "getxattr");
    real_lgetxattr = dlsym(RTLD_NEXT, "lgetxattr");
    real_setxattr = dlsym(RTLD_NEXT, "setxattr");
    real_lsetxattr = dlsym(RTLD_NEXT, "lsetxattr");
}

static int classify(const char *name) {
    if (!name) return K_OTHER;
    for (int i = 0; i < K_OTHER; i++) {
        if (strcmp(name, KEY_NAMES[i]) == 0) return i;
    }
    return K_OTHER;
}

/* Interposed libc calls. Always defer to the real implementation;
 * we only count when enabled. */
ssize_t getxattr(const char *path, const char *name, void *value, size_t size) {
    pthread_once(&g_dlsym_once, resolve_real);
    if (g_enabled) {
        atomic_fetch_add_explicit(&g_get_count[classify(name)], 1,
                                  memory_order_relaxed);
    }
    return real_getxattr ? real_getxattr(path, name, value, size)
                         : (errno = ENOSYS, -1);
}

ssize_t lgetxattr(const char *path, const char *name, void *value, size_t size) {
    pthread_once(&g_dlsym_once, resolve_real);
    if (g_enabled) {
        atomic_fetch_add_explicit(&g_get_count[classify(name)], 1,
                                  memory_order_relaxed);
    }
    return real_lgetxattr ? real_lgetxattr(path, name, value, size)
                          : (errno = ENOSYS, -1);
}

int setxattr(const char *path, const char *name, const void *value,
             size_t size, int flags) {
    pthread_once(&g_dlsym_once, resolve_real);
    if (g_enabled) {
        atomic_fetch_add_explicit(&g_set_count[classify(name)], 1,
                                  memory_order_relaxed);
    }
    return real_setxattr ? real_setxattr(path, name, value, size, flags)
                         : (errno = ENOSYS, -1);
}

int lsetxattr(const char *path, const char *name, const void *value,
              size_t size, int flags) {
    pthread_once(&g_dlsym_once, resolve_real);
    if (g_enabled) {
        atomic_fetch_add_explicit(&g_set_count[classify(name)], 1,
                                  memory_order_relaxed);
    }
    return real_lsetxattr ? real_lsetxattr(path, name, value, size, flags)
                          : (errno = ENOSYS, -1);
}

/* Reporter thread — once per second, logs the delta since last tick. */
static void *reporter_main(void *arg) {
    (void)arg;
    uint64_t last_get[N_KEY_BUCKETS] = {0};
    uint64_t last_set[N_KEY_BUCKETS] = {0};
    LOG_INFO("xattr", "reporter started; flag=%s", XATTR_COUNT_FLAG_PATH);

    while (1) {
        struct timespec ts = { .tv_sec = 1, .tv_nsec = 0 };
        nanosleep(&ts, NULL);

        int now_enabled = (access(XATTR_COUNT_FLAG_PATH, F_OK) == 0);
        if (now_enabled != g_enabled) {
            LOG_INFO("xattr", "counter %s", now_enabled ? "ENABLED" : "DISABLED");
            g_enabled = now_enabled;
            /* On re-enable, reset the "last" so we start fresh */
            if (now_enabled) {
                for (int i = 0; i < N_KEY_BUCKETS; i++) {
                    last_get[i] = atomic_load(&g_get_count[i]);
                    last_set[i] = atomic_load(&g_set_count[i]);
                }
            }
        }
        if (!g_enabled) continue;

        /* Compute deltas and emit a single combined log line. */
        char line[512];
        int off = 0;
        uint64_t total_get = 0, total_set = 0;
        for (int i = 0; i < N_KEY_BUCKETS; i++) {
            uint64_t g_now = atomic_load(&g_get_count[i]);
            uint64_t s_now = atomic_load(&g_set_count[i]);
            uint64_t dg = g_now - last_get[i];
            uint64_t ds = s_now - last_set[i];
            last_get[i] = g_now;
            last_set[i] = s_now;
            total_get += dg;
            total_set += ds;
            if (dg || ds) {
                const char *short_name = KEY_NAMES[i];
                /* Strip "user." prefix for compactness */
                if (strncmp(short_name, "user.", 5) == 0) short_name += 5;
                off += snprintf(line + off, sizeof(line) - off,
                                " %s=g%llu/s%llu", short_name,
                                (unsigned long long)dg, (unsigned long long)ds);
                if (off >= (int)sizeof(line) - 32) break;
            }
        }
        if (total_get || total_set) {
            LOG_INFO("xattr", "1s: get=%llu set=%llu  [%s ]",
                     (unsigned long long)total_get,
                     (unsigned long long)total_set, line);
        }
    }
    return NULL;
}

__attribute__((constructor(102)))   /* run before sampling_profiler ctor */
static void xattr_counter_init(void) {
    pthread_once(&g_dlsym_once, resolve_real);
    g_enabled = (access(XATTR_COUNT_FLAG_PATH, F_OK) == 0);
    pthread_t t;
    if (pthread_create(&t, NULL, reporter_main, NULL) == 0) {
        pthread_detach(t);
    }
}
