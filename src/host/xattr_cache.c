/*
 * xattr_cache.c — caching LD_PRELOAD interposer for Move's
 * filesystem xattr walker, with inotify(IN_ATTRIB) invalidation.
 *
 * Background
 * ----------
 * Move's main thread spends ~25% of one ARM core walking
 * /data/UserData/UserLibrary/Sets/<UUID>/ and reading 5 song-state
 * xattrs per set every ~2 ms (32,200 getxattr/sec on the test
 * device with 14 sets). MoveOriginal has zero inotify usage; the
 * walks are on-demand from Browser/SongWheel view-model rebuilds.
 * See docs/move-firmware-investigation-2026-05-19.md.
 *
 * This file interposes libc getxattr/lgetxattr/setxattr/lsetxattr
 * and answers from a process-local cache. Invalidation channels:
 *
 *   1. Move's own setxattr — we update the cache in-line.
 *   2. inotify(IN_ATTRIB | IN_MODIFY | IN_DELETE_SELF | IN_MOVE_SELF)
 *      on each watched dir — catches writers in OTHER processes
 *      (e.g. the Cloud sync daemon that touches user.local-cloud-state).
 *   3. inotify(IN_CREATE | IN_DELETE | IN_MOVED_FROM | IN_MOVED_TO)
 *      on /data/UserData/UserLibrary/Sets/ parent — catches set
 *      creation/deletion/rename.
 *   4. IN_Q_OVERFLOW — flush everything.
 *
 * Two flag files control behavior independently:
 *
 *   /data/UserData/schwung/xattr_count_on
 *     - enables passive call counting (hit/miss/cache breakdown
 *       reported once per second to the unified log)
 *
 *   /data/UserData/schwung/xattr_cache_on
 *     - enables the cache itself. When flipped off, the cache is
 *       flushed and all calls go straight through.
 *
 * Both flags are checked once per second by a background thread,
 * so the hot path is a single atomic int read.
 *
 * This is NOT in upstream schwung — it's a test/research mitigation
 * for a Move-firmware bug. Risks: external xattr writers must hit
 * one of our invalidation channels for the cache to stay coherent;
 * we handle the cloud sync daemon via inotify, but if some other
 * process modifies xattrs in a way we don't see (filesystem
 * unmount/remount, kernel-bypass, etc), the cache could go stale.
 * Mitigation: just turn off the flag.
 */

#define _GNU_SOURCE
#include "unified_log.h"

#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/inotify.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/xattr.h>
#include <time.h>
#include <unistd.h>

#define XATTR_COUNT_FLAG_PATH "/data/UserData/schwung/xattr_count_on"
#define XATTR_CACHE_FLAG_PATH "/data/UserData/schwung/xattr_cache_on"
#define WATCH_ROOT            "/data/UserData/UserLibrary/Sets"

/* The 5 known song-state keys subagent #3 identified, plus an
 * <other> bucket for everything else (NOT cached, just counted). */
enum {
    K_LAST_MODIFIED, K_CLOUD_STATE, K_SONG_COLOR, K_SONG_INDEX,
    K_EXTERNALLY_MODIFIED, K_OTHER, N_KEY_BUCKETS
};
static const char *KEY_NAMES[N_KEY_BUCKETS] = {
    "user.last-modified-time",
    "user.local-cloud-state",
    "user.song-color",
    "user.song-index",
    "user.was-externally-modified",
    "<other>",
};

/* ------------------------------------------------------------------ */
/* Counters (always-on, atomic). Pretty much like the old xattr_counter. */

static atomic_uint_least64_t g_get_count[N_KEY_BUCKETS];
static atomic_uint_least64_t g_set_count[N_KEY_BUCKETS];
static atomic_uint_least64_t g_cache_hits[N_KEY_BUCKETS];
static atomic_uint_least64_t g_cache_neg_hits;   /* ENODATA cached */
static atomic_uint_least64_t g_cache_invalidations;
static atomic_uint_least64_t g_cache_stores;
static atomic_uint_least64_t g_inotify_events;

/* ------------------------------------------------------------------ */
/* Real libc functions, resolved lazily via dlsym. */

static ssize_t (*real_getxattr)(const char *, const char *, void *, size_t);
static ssize_t (*real_lgetxattr)(const char *, const char *, void *, size_t);
static int     (*real_setxattr)(const char *, const char *, const void *, size_t, int);
static int     (*real_lsetxattr)(const char *, const char *, const void *, size_t, int);
static pthread_once_t g_dlsym_once = PTHREAD_ONCE_INIT;

static void resolve_real(void) {
    real_getxattr  = dlsym(RTLD_NEXT, "getxattr");
    real_lgetxattr = dlsym(RTLD_NEXT, "lgetxattr");
    real_setxattr  = dlsym(RTLD_NEXT, "setxattr");
    real_lsetxattr = dlsym(RTLD_NEXT, "lsetxattr");
}

/* ------------------------------------------------------------------ */
/* Flag bits, updated once per second by reporter thread.
 * Hot path is one volatile int load. */

static volatile int g_count_enabled = 0;
static volatile int g_cache_enabled = 0;

/* ------------------------------------------------------------------ */
/* Cache: fixed-size open-addressing hash table. We expect on the
 * order of N_sets * 5 entries (~70 on the test device). 1024 buckets
 * is comfortable. Entries store the path (heap-allocated, owned),
 * the key index, and the raw value bytes inline (up to MAX_VALUE_SIZE).
 *
 * size == -1 means "negative cache" (the xattr is known to be missing,
 * return ENODATA). size >= 0 is a real value. path == NULL is "slot empty".
 */

#define BUCKETS         1024
#define MAX_VALUE_SIZE  256       /* xattr values are tiny; 256 is overkill */

struct cache_entry {
    char    *path;        /* owned. NULL = empty slot */
    int      key_idx;     /* 0..K_OTHER-1 */
    ssize_t  size;        /* -1 = neg cache, >=0 = bytes in value[] */
    char     value[MAX_VALUE_SIZE];
};

static struct cache_entry g_cache[BUCKETS];
static pthread_rwlock_t   g_cache_lock = PTHREAD_RWLOCK_INITIALIZER;

static int classify_key(const char *name) {
    if (!name) return K_OTHER;
    for (int i = 0; i < K_OTHER; i++)
        if (strcmp(name, KEY_NAMES[i]) == 0) return i;
    return K_OTHER;
}

static uint64_t hash_path_key(const char *path, int key_idx) {
    /* FNV-1a 64. */
    uint64_t h = 0xcbf29ce484222325ULL;
    while (*path) { h ^= (uint8_t)*path++; h *= 0x100000001b3ULL; }
    h ^= '/';                       h *= 0x100000001b3ULL;
    h ^= (uint8_t)key_idx;          h *= 0x100000001b3ULL;
    return h;
}

/* Returns bucket idx (always valid: linear-probes from hash; will
 * land on either a matching entry or the first empty slot). */
static int cache_lookup_slot(const char *path, int key_idx) {
    uint64_t h = hash_path_key(path, key_idx);
    int start = (int)(h % BUCKETS);
    for (int probe = 0; probe < BUCKETS; probe++) {
        int idx = (start + probe) % BUCKETS;
        if (g_cache[idx].path == NULL) return idx;
        if (g_cache[idx].key_idx == key_idx
                && strcmp(g_cache[idx].path, path) == 0)
            return idx;
    }
    return -1;  /* table full — practically impossible at 1024 slots */
}

/* Invalidate all cached entries whose path equals `path` (we cache
 * only by full path, so this is a strcmp against each occupied slot).
 * Caller must hold WRITE lock on g_cache_lock. Returns the count freed. */
static int invalidate_path_unlocked(const char *path) {
    int count = 0;
    for (int i = 0; i < BUCKETS; i++) {
        if (g_cache[i].path && strcmp(g_cache[i].path, path) == 0) {
            free(g_cache[i].path);
            g_cache[i].path = NULL;
            count++;
        }
    }
    return count;
}

static void cache_flush_all(void) {
    pthread_rwlock_wrlock(&g_cache_lock);
    int n = 0;
    for (int i = 0; i < BUCKETS; i++) {
        if (g_cache[i].path) {
            free(g_cache[i].path);
            g_cache[i].path = NULL;
            n++;
        }
    }
    pthread_rwlock_unlock(&g_cache_lock);
    LOG_INFO("xattrc", "cache_flush_all: %d entries freed", n);
}

/* ------------------------------------------------------------------ */
/* inotify state. */

static int       g_inotify_fd  = -1;
static pthread_t g_inotify_thread;
static int       g_parent_wd   = -1;

#define MAX_WATCHES 256
struct watch_entry { int wd; char *path; };
static struct watch_entry g_watches[MAX_WATCHES];
static int               g_n_watches = 0;
static pthread_mutex_t   g_watches_lock = PTHREAD_MUTEX_INITIALIZER;

/* Add a watch on `path` if not already present. Called from hot path
 * (getxattr miss) so keep cheap. inotify_add_watch is a syscall but
 * IS idempotent — if we re-add an existing watch it just returns the
 * same wd. We dedupe in userspace anyway to avoid the syscall. */
static void watch_ensure(const char *path) {
    if (g_inotify_fd < 0) return;
    pthread_mutex_lock(&g_watches_lock);
    for (int i = 0; i < g_n_watches; i++) {
        if (strcmp(g_watches[i].path, path) == 0) {
            pthread_mutex_unlock(&g_watches_lock);
            return;
        }
    }
    if (g_n_watches >= MAX_WATCHES) {
        pthread_mutex_unlock(&g_watches_lock);
        return;
    }
    int wd = inotify_add_watch(g_inotify_fd, path,
        IN_ATTRIB | IN_MODIFY | IN_DELETE_SELF | IN_MOVE_SELF);
    if (wd < 0) {
        pthread_mutex_unlock(&g_watches_lock);
        return;
    }
    g_watches[g_n_watches].wd = wd;
    g_watches[g_n_watches].path = strdup(path);
    g_n_watches++;
    pthread_mutex_unlock(&g_watches_lock);
}

/* Remove watch (called from inotify thread on IN_DELETE_SELF /
 * IN_MOVE_SELF / parent IN_DELETE). Also invalidates cache for
 * that path. */
static void watch_remove_by_wd(int wd) {
    pthread_mutex_lock(&g_watches_lock);
    char *path_to_invalidate = NULL;
    for (int i = 0; i < g_n_watches; i++) {
        if (g_watches[i].wd == wd) {
            path_to_invalidate = g_watches[i].path;
            inotify_rm_watch(g_inotify_fd, wd);
            /* swap-with-last */
            g_n_watches--;
            g_watches[i].wd = g_watches[g_n_watches].wd;
            g_watches[i].path = g_watches[g_n_watches].path;
            break;
        }
    }
    pthread_mutex_unlock(&g_watches_lock);
    if (path_to_invalidate) {
        pthread_rwlock_wrlock(&g_cache_lock);
        int n = invalidate_path_unlocked(path_to_invalidate);
        pthread_rwlock_unlock(&g_cache_lock);
        if (n) atomic_fetch_add(&g_cache_invalidations, (uint64_t)n);
        free(path_to_invalidate);
    }
}

/* On any child-event of a watched dir, invalidate cache for that dir.
 * Don't remove the watch (the dir still exists). */
static void invalidate_by_wd(int wd) {
    pthread_mutex_lock(&g_watches_lock);
    char *path = NULL;
    for (int i = 0; i < g_n_watches; i++) {
        if (g_watches[i].wd == wd) {
            path = strdup(g_watches[i].path);
            break;
        }
    }
    pthread_mutex_unlock(&g_watches_lock);
    if (!path) return;
    pthread_rwlock_wrlock(&g_cache_lock);
    int n = invalidate_path_unlocked(path);
    pthread_rwlock_unlock(&g_cache_lock);
    if (n) atomic_fetch_add(&g_cache_invalidations, (uint64_t)n);
    free(path);
}

/* ------------------------------------------------------------------ */
/* Cache get/store. Returns special sentinel -2 from try_get to mean
 * "miss; caller must fall through to real syscall + store". */

#define MISS_SENTINEL ((ssize_t)-2)

static ssize_t cache_try_get(const char *path, int key_idx,
                             void *value, size_t size) {
    if (key_idx == K_OTHER) return MISS_SENTINEL;
    pthread_rwlock_rdlock(&g_cache_lock);
    int idx = cache_lookup_slot(path, key_idx);
    ssize_t result = MISS_SENTINEL;
    if (idx >= 0 && g_cache[idx].path != NULL) {
        if (g_cache[idx].size == -1) {
            /* negative cache hit */
            atomic_fetch_add_explicit(&g_cache_neg_hits, 1, memory_order_relaxed);
            atomic_fetch_add_explicit(&g_cache_hits[key_idx], 1, memory_order_relaxed);
            errno = ENODATA;
            result = -1;
        } else {
            atomic_fetch_add_explicit(&g_cache_hits[key_idx], 1, memory_order_relaxed);
            ssize_t entry_size = g_cache[idx].size;
            if (value == NULL || size == 0) {
                /* querying size only — same semantic as real getxattr */
                result = entry_size;
            } else if (size < (size_t)entry_size) {
                errno = ERANGE;
                result = -1;
            } else {
                memcpy(value, g_cache[idx].value, (size_t)entry_size);
                result = entry_size;
            }
        }
    }
    pthread_rwlock_unlock(&g_cache_lock);
    return result;
}

static void cache_store(const char *path, int key_idx,
                        const void *value, ssize_t size) {
    if (key_idx == K_OTHER) return;
    if (size > MAX_VALUE_SIZE) return;  /* too big to cache; pass through */
    pthread_rwlock_wrlock(&g_cache_lock);
    int idx = cache_lookup_slot(path, key_idx);
    if (idx >= 0) {
        if (g_cache[idx].path == NULL) {
            g_cache[idx].path = strdup(path);
            g_cache[idx].key_idx = key_idx;
        }
        g_cache[idx].size = size;
        if (size > 0 && value) memcpy(g_cache[idx].value, value, (size_t)size);
        atomic_fetch_add_explicit(&g_cache_stores, 1, memory_order_relaxed);
    }
    pthread_rwlock_unlock(&g_cache_lock);
    /* Ensure inotify watch exists on this path. Idempotent. */
    watch_ensure(path);
}

/* ------------------------------------------------------------------ */
/* Interposed libc functions.
 *
 * Generic flow:
 *   1. Always count (cheap, atomic).
 *   2. If cache disabled OR key unknown OR no path → fall through.
 *   3. Cache lookup. Hit → return cached.
 *   4. Miss → call real syscall with a scratch buf big enough for
 *      our cache slot. On success, cache the result. On ENODATA,
 *      negative-cache. Copy out to caller's buffer respecting their
 *      size argument exactly as the real syscall would.
 */

static ssize_t do_get(int is_lget, const char *path, const char *name,
                      void *value, size_t size) {
    pthread_once(&g_dlsym_once, resolve_real);
    int kidx = classify_key(name);
    if (g_count_enabled)
        atomic_fetch_add_explicit(&g_get_count[kidx], 1, memory_order_relaxed);

    if (!g_cache_enabled || kidx == K_OTHER || path == NULL || name == NULL) {
        if (is_lget) {
            return real_lgetxattr ? real_lgetxattr(path, name, value, size)
                                  : (errno = ENOSYS, -1);
        } else {
            return real_getxattr  ? real_getxattr(path, name, value, size)
                                  : (errno = ENOSYS, -1);
        }
    }

    ssize_t r = cache_try_get(path, kidx, value, size);
    if (r != MISS_SENTINEL) return r;

    /* Miss: read real value into a scratch buffer (big enough), cache,
     * then copy to caller respecting their size argument. */
    char scratch[MAX_VALUE_SIZE];
    ssize_t real_r = is_lget
        ? (real_lgetxattr ? real_lgetxattr(path, name, scratch, sizeof(scratch)) : -1)
        : (real_getxattr  ? real_getxattr(path,  name, scratch, sizeof(scratch)) : -1);
    int saved_errno = errno;
    if (real_r >= 0) {
        cache_store(path, kidx, scratch, real_r);
        /* Echo real syscall's size/EAGAIN semantics. */
        if (value == NULL || size == 0) return real_r;
        if ((size_t)real_r > size) { errno = ERANGE; return -1; }
        memcpy(value, scratch, (size_t)real_r);
        return real_r;
    }
    if (saved_errno == ENODATA) {
        cache_store(path, kidx, NULL, -1);  /* negative cache */
    }
    errno = saved_errno;
    return -1;
}

static int do_set(int is_lset, const char *path, const char *name,
                  const void *value, size_t size, int flags) {
    pthread_once(&g_dlsym_once, resolve_real);
    int kidx = classify_key(name);
    if (g_count_enabled)
        atomic_fetch_add_explicit(&g_set_count[kidx], 1, memory_order_relaxed);

    int r = is_lset
        ? (real_lsetxattr ? real_lsetxattr(path, name, value, size, flags) : (errno = ENOSYS, -1))
        : (real_setxattr  ? real_setxattr(path,  name, value, size, flags) : (errno = ENOSYS, -1));
    /* If our cache holds this (path, key), refresh it with the new
     * value. Do this regardless of whether cache is enabled, because
     * flipping the flag off doesn't clear the table — staleness
     * after re-enable would be a surprise. */
    if (r == 0 && kidx != K_OTHER && path && size <= MAX_VALUE_SIZE) {
        cache_store(path, kidx, value, (ssize_t)size);
    }
    return r;
}

ssize_t getxattr(const char *path, const char *name, void *value, size_t size) {
    return do_get(0, path, name, value, size);
}
ssize_t lgetxattr(const char *path, const char *name, void *value, size_t size) {
    return do_get(1, path, name, value, size);
}
int setxattr(const char *path, const char *name, const void *value,
             size_t size, int flags) {
    return do_set(0, path, name, value, size, flags);
}
int lsetxattr(const char *path, const char *name, const void *value,
              size_t size, int flags) {
    return do_set(1, path, name, value, size, flags);
}

/* ------------------------------------------------------------------ */
/* inotify event loop. */

static void *inotify_main(void *arg) {
    (void)arg;
    /* Watch the parent dir for set creation/deletion/rename. */
    if (g_inotify_fd >= 0) {
        g_parent_wd = inotify_add_watch(g_inotify_fd, WATCH_ROOT,
            IN_CREATE | IN_DELETE | IN_MOVED_FROM | IN_MOVED_TO);
        if (g_parent_wd < 0)
            LOG_WARN("xattrc", "could not watch %s: %s", WATCH_ROOT, strerror(errno));
        else
            LOG_INFO("xattrc", "watching parent %s wd=%d", WATCH_ROOT, g_parent_wd);
    }

    /* Per inotify(7): buffer must be large enough for at least one
     * event. We size for several. */
    char buf[8192] __attribute__((aligned(8)));
    while (1) {
        ssize_t len = read(g_inotify_fd, buf, sizeof(buf));
        if (len <= 0) {
            if (errno == EINTR) continue;
            LOG_WARN("xattrc", "inotify read failed: %s", strerror(errno));
            break;
        }
        char *p = buf;
        while (p < buf + len) {
            struct inotify_event *ev = (struct inotify_event *)p;
            atomic_fetch_add_explicit(&g_inotify_events, 1, memory_order_relaxed);

            if (ev->mask & IN_Q_OVERFLOW) {
                LOG_WARN("xattrc", "IN_Q_OVERFLOW — flushing cache");
                cache_flush_all();
            } else if (ev->wd == g_parent_wd) {
                /* parent dir event: child created/deleted/moved */
                if (ev->len > 0 && (ev->mask & (IN_DELETE | IN_MOVED_FROM))) {
                    char full[600];
                    snprintf(full, sizeof(full), "%s/%s", WATCH_ROOT, ev->name);
                    /* Find watch on the child (if any) and remove it */
                    pthread_mutex_lock(&g_watches_lock);
                    int found_wd = -1;
                    for (int i = 0; i < g_n_watches; i++) {
                        /* Watched path is either exactly `full` or
                         * `full/` with trailing slash — match both */
                        size_t flen = strlen(full);
                        const char *wp = g_watches[i].path;
                        if (strncmp(wp, full, flen) == 0
                                && (wp[flen] == '\0' || wp[flen] == '/')) {
                            found_wd = g_watches[i].wd;
                            break;
                        }
                    }
                    pthread_mutex_unlock(&g_watches_lock);
                    if (found_wd >= 0) watch_remove_by_wd(found_wd);
                    /* Also invalidate any cache entries for exact path */
                    pthread_rwlock_wrlock(&g_cache_lock);
                    int n = invalidate_path_unlocked(full);
                    pthread_rwlock_unlock(&g_cache_lock);
                    if (n) atomic_fetch_add(&g_cache_invalidations, (uint64_t)n);
                }
                /* IN_CREATE / IN_MOVED_TO: defer to lazy watch_ensure
                 * on the next getxattr for that path. */
            } else {
                /* event on a watched child dir */
                if (ev->mask & (IN_DELETE_SELF | IN_MOVE_SELF)) {
                    watch_remove_by_wd(ev->wd);
                } else {
                    /* IN_ATTRIB / IN_MODIFY: invalidate cache for this path */
                    invalidate_by_wd(ev->wd);
                }
            }
            p += sizeof(*ev) + ev->len;
        }
    }
    return NULL;
}

/* ------------------------------------------------------------------ */
/* Reporter thread: polls flag files, emits stats once per second. */

static void *reporter_main(void *arg) {
    (void)arg;
    uint64_t last_get[N_KEY_BUCKETS] = {0};
    uint64_t last_set[N_KEY_BUCKETS] = {0};
    uint64_t last_hits[N_KEY_BUCKETS] = {0};
    uint64_t last_neg = 0, last_inv = 0, last_store = 0, last_ino = 0;
    LOG_INFO("xattrc", "reporter started; flags: count=%s cache=%s",
             XATTR_COUNT_FLAG_PATH, XATTR_CACHE_FLAG_PATH);

    while (1) {
        struct timespec ts = { .tv_sec = 1, .tv_nsec = 0 };
        nanosleep(&ts, NULL);

        int new_count = (access(XATTR_COUNT_FLAG_PATH, F_OK) == 0);
        int new_cache = (access(XATTR_CACHE_FLAG_PATH, F_OK) == 0);
        if (new_count != g_count_enabled) {
            LOG_INFO("xattrc", "counter %s", new_count ? "ENABLED" : "DISABLED");
            g_count_enabled = new_count;
            if (new_count) {
                /* reset deltas to start fresh */
                for (int i = 0; i < N_KEY_BUCKETS; i++) {
                    last_get[i] = atomic_load(&g_get_count[i]);
                    last_set[i] = atomic_load(&g_set_count[i]);
                    last_hits[i] = atomic_load(&g_cache_hits[i]);
                }
                last_neg = atomic_load(&g_cache_neg_hits);
                last_inv = atomic_load(&g_cache_invalidations);
                last_store = atomic_load(&g_cache_stores);
                last_ino = atomic_load(&g_inotify_events);
            }
        }
        if (new_cache != g_cache_enabled) {
            LOG_INFO("xattrc", "cache %s", new_cache ? "ENABLED" : "DISABLED");
            g_cache_enabled = new_cache;
            if (!new_cache) cache_flush_all();
        }

        if (!g_count_enabled) continue;

        uint64_t dget_total = 0, dset_total = 0, dhit_total = 0;
        for (int i = 0; i < N_KEY_BUCKETS; i++) {
            uint64_t gnow = atomic_load(&g_get_count[i]);
            uint64_t snow = atomic_load(&g_set_count[i]);
            uint64_t hnow = atomic_load(&g_cache_hits[i]);
            dget_total += gnow - last_get[i];
            dset_total += snow - last_set[i];
            dhit_total += hnow - last_hits[i];
            last_get[i] = gnow;
            last_set[i] = snow;
            last_hits[i] = hnow;
        }
        uint64_t neg_now = atomic_load(&g_cache_neg_hits);
        uint64_t inv_now = atomic_load(&g_cache_invalidations);
        uint64_t store_now = atomic_load(&g_cache_stores);
        uint64_t ino_now = atomic_load(&g_inotify_events);
        uint64_t dneg = neg_now - last_neg;
        uint64_t dinv = inv_now - last_inv;
        uint64_t dstore = store_now - last_store;
        uint64_t dino = ino_now - last_ino;
        last_neg = neg_now; last_inv = inv_now;
        last_store = store_now; last_ino = ino_now;

        if (dget_total || dset_total || dhit_total) {
            uint64_t denom = dget_total ? dget_total : 1;
            int hit_pct = (int)((dhit_total * 100) / denom);
            LOG_INFO("xattrc",
                "1s: get=%llu set=%llu hit=%llu(%d%%) neg=%llu store=%llu inv=%llu ino_ev=%llu",
                (unsigned long long)dget_total,
                (unsigned long long)dset_total,
                (unsigned long long)dhit_total, hit_pct,
                (unsigned long long)dneg,
                (unsigned long long)dstore,
                (unsigned long long)dinv,
                (unsigned long long)dino);
        }
    }
    return NULL;
}

/* ------------------------------------------------------------------ */

__attribute__((constructor(102)))
static void xattr_cache_init(void) {
    pthread_once(&g_dlsym_once, resolve_real);
    g_count_enabled = (access(XATTR_COUNT_FLAG_PATH, F_OK) == 0);
    g_cache_enabled = (access(XATTR_CACHE_FLAG_PATH, F_OK) == 0);

    /* Open the inotify fd up front. The watcher thread relies on
     * fd >= 0; we can survive if this fails (cache works without
     * inotify, just without external-writer invalidation). */
    g_inotify_fd = inotify_init1(IN_CLOEXEC);
    if (g_inotify_fd < 0) {
        LOG_WARN("xattrc", "inotify_init1 failed: %s — cache will run without external invalidation",
                 strerror(errno));
    }

    pthread_t t;
    if (g_inotify_fd >= 0
            && pthread_create(&t, NULL, inotify_main, NULL) == 0) {
        pthread_detach(t);
    }
    if (pthread_create(&t, NULL, reporter_main, NULL) == 0) {
        pthread_detach(t);
    }
}
