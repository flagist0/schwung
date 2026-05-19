/*
 * sampling_profiler.c — see sampling_profiler.h for the why.
 *
 * On-disk format (little-endian, matches host build of parser):
 *
 *   magic[8]      = "SPROF\0\0\0"
 *   uint32 ver    = 1
 *   uint32 hz     = sample period in Hz (1000)
 *   uint64 t0_ns  = monotonic clock at start (for timestamp rebase)
 *   uint32 maps_len
 *   char   maps[maps_len]   (verbatim /proc/self/maps content)
 *   --- repeating records ---
 *     uint8 record_type
 *       == 0x53 (S): sample
 *         uint64 ip
 *         uint32 tid
 *         uint64 time_ns
 *         uint16 nr_pc
 *         uint64 pc[nr_pc]   (callchain, innermost first)
 *       == 0x4D (M): mmap update (new lib loaded mid-run; not implemented yet)
 *       == 0x45 (E): end marker (clean shutdown)
 *
 * The parser side: read maps into a (start,end,filename,offset)
 * list, bsearch each PC, emit `lib:offset` (or `function+0xN` if
 * we have addr2line on the host).
 */

#include "sampling_profiler.h"
#include "unified_log.h"

#include <errno.h>
#include <fcntl.h>
#include <linux/perf_event.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#define SAMPLE_HZ            1000          /* 1 kHz */
#define RING_DATA_PAGES      64            /* 256 KB ring */
#define READER_POLL_MS       100           /* drain ring every 100ms */
#define MAX_CALLCHAIN        32            /* kernel default cap */

static struct {
    int                fd;
    void              *mmap_base;
    size_t             mmap_size;
    size_t             data_size;          /* ring excluding metadata page */
    pthread_t          reader;
    volatile int       stop;
    FILE              *out;
    uint64_t           samples_written;
    uint64_t           samples_lost;
    pthread_mutex_t    out_mtx;
} g = { .fd = -1 };

static long sys_perf_event_open(struct perf_event_attr *attr, pid_t pid,
                                int cpu, int group_fd, unsigned long flags) {
    return syscall(__NR_perf_event_open, attr, pid, cpu, group_fd, flags);
}

static int file_exists(const char *p) {
    return access(p, F_OK) == 0;
}

static uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

/* Dump /proc/self/maps into the output. Called once at init. */
static int write_maps_header(FILE *out) {
    FILE *mf = fopen("/proc/self/maps", "r");
    if (!mf) return -1;
    /* Slurp first; we need the length before writing the record. */
    char buf[65536];
    size_t total = 0;
    int c;
    while (total < sizeof(buf) - 1 && (c = fgetc(mf)) != EOF) {
        buf[total++] = (char)c;
    }
    fclose(mf);
    buf[total] = 0;
    uint32_t len = (uint32_t)total;
    fwrite(&len, sizeof(len), 1, out);
    fwrite(buf, 1, total, out);
    return 0;
}

/* Walk the ring, write each sample to file. Called from reader thread. */
static void drain_ring(void) {
    struct perf_event_mmap_page *mp =
        (struct perf_event_mmap_page *)g.mmap_base;
    uint64_t head = __atomic_load_n(&mp->data_head, __ATOMIC_ACQUIRE);
    uint64_t tail = mp->data_tail;
    if (tail == head) return;

    char *data = (char *)g.mmap_base + sysconf(_SC_PAGESIZE);
    size_t ds = g.data_size;

    /* Bounce buffer for records that wrap the ring boundary. */
    char scratch[4096];

    pthread_mutex_lock(&g.out_mtx);
    while (tail < head) {
        struct perf_event_header h_hdr;
        size_t off = tail % ds;
        if (off + sizeof(h_hdr) <= ds) {
            memcpy(&h_hdr, data + off, sizeof(h_hdr));
        } else {
            /* Header wraps — copy in two pieces */
            size_t a = ds - off;
            memcpy(&h_hdr, data + off, a);
            memcpy((char *)&h_hdr + a, data, sizeof(h_hdr) - a);
        }
        if (h_hdr.size == 0 || h_hdr.size > sizeof(scratch)) {
            /* Corruption or oversized record; bail. */
            break;
        }
        char *rec;
        if (off + h_hdr.size <= ds) {
            rec = data + off;
        } else {
            size_t a = ds - off;
            memcpy(scratch, data + off, a);
            memcpy(scratch + a, data, h_hdr.size - a);
            rec = scratch;
        }
        if (h_hdr.type == PERF_RECORD_SAMPLE) {
            /* Layout per attr.sample_type below:
             *   PERF_SAMPLE_IP       -> u64 ip
             *   PERF_SAMPLE_TID      -> u32 pid, u32 tid
             *   PERF_SAMPLE_TIME     -> u64 time
             *   PERF_SAMPLE_CALLCHAIN-> u64 nr, u64 pc[nr]
             */
            char *p = rec + sizeof(h_hdr);
            uint64_t ip      = *(uint64_t *)p; p += 8;
            uint32_t pid_    = *(uint32_t *)p; p += 4;
            uint32_t tid     = *(uint32_t *)p; p += 4;
            uint64_t t       = *(uint64_t *)p; p += 8;
            uint64_t nr      = *(uint64_t *)p; p += 8;
            if (nr > MAX_CALLCHAIN) nr = MAX_CALLCHAIN;
            (void)pid_;

            uint8_t tag = 'S';
            uint16_t nr16 = (uint16_t)nr;
            fwrite(&tag, 1, 1, g.out);
            fwrite(&ip, 8, 1, g.out);
            fwrite(&tid, 4, 1, g.out);
            fwrite(&t, 8, 1, g.out);
            fwrite(&nr16, 2, 1, g.out);
            fwrite(p, 8, (size_t)nr, g.out);
            g.samples_written++;
        } else if (h_hdr.type == PERF_RECORD_LOST) {
            /* struct lost { u64 id; u64 lost; } */
            char *p = rec + sizeof(h_hdr);
            uint64_t lost = *(uint64_t *)(p + 8);
            g.samples_lost += lost;
        }
        tail += h_hdr.size;
    }
    __atomic_store_n(&mp->data_tail, tail, __ATOMIC_RELEASE);
    fflush(g.out);
    pthread_mutex_unlock(&g.out_mtx);
}

static void *reader_main(void *arg) {
    (void)arg;
    LOG_INFO("sprof", "reader thread started");
    /* Check the flag file every ~1s and self-stop if it disappears,
     * so we don't need restart_move to end a profiling session. */
    int flag_check_counter = 0;
    while (!g.stop) {
        drain_ring();
        struct timespec ts = { .tv_sec = 0, .tv_nsec = READER_POLL_MS * 1000000L };
        nanosleep(&ts, NULL);
        if (++flag_check_counter * READER_POLL_MS >= 1000) {
            flag_check_counter = 0;
            if (!file_exists(SAMPLING_PROFILER_FLAG_PATH)) {
                LOG_INFO("sprof", "flag file removed, stopping");
                g.stop = 1;
                break;
            }
        }
    }
    drain_ring();  /* final flush */
    /* Also disable + close perf fd so the kernel stops sampling */
    if (g.fd >= 0) {
        ioctl(g.fd, PERF_EVENT_IOC_DISABLE, 0);
    }
    LOG_INFO("sprof", "reader thread exiting: %llu samples written, %llu lost",
             (unsigned long long)g.samples_written,
             (unsigned long long)g.samples_lost);
    return NULL;
}

int sampling_profiler_init(void) {
    if (!file_exists(SAMPLING_PROFILER_FLAG_PATH)) {
        return 1;  /* not an error; just disabled */
    }

    g.out = fopen(SAMPLING_PROFILER_OUT_PATH, "wb");
    if (!g.out) {
        LOG_ERROR("sprof", "cannot open output %s: %s",
                  SAMPLING_PROFILER_OUT_PATH, strerror(errno));
        return -1;
    }
    pthread_mutex_init(&g.out_mtx, NULL);

    /* Header: magic, version, hz, t0 */
    fwrite("SPROF\0\0\0", 1, 8, g.out);
    uint32_t ver = 1;     fwrite(&ver, 4, 1, g.out);
    uint32_t hz  = SAMPLE_HZ; fwrite(&hz, 4, 1, g.out);
    uint64_t t0  = now_ns(); fwrite(&t0, 8, 1, g.out);
    if (write_maps_header(g.out) != 0) {
        LOG_ERROR("sprof", "cannot read /proc/self/maps");
        fclose(g.out); g.out = NULL;
        return -1;
    }

    /* Open perf event for THIS thread (constructor → main thread). */
    struct perf_event_attr a = {0};
    a.type           = PERF_TYPE_SOFTWARE;
    a.size           = sizeof(a);
    a.config         = PERF_COUNT_SW_CPU_CLOCK;
    a.sample_freq    = SAMPLE_HZ;
    a.freq           = 1;
    a.sample_type    = PERF_SAMPLE_IP | PERF_SAMPLE_TID |
                       PERF_SAMPLE_TIME | PERF_SAMPLE_CALLCHAIN;
    a.exclude_kernel = 1;   /* required at paranoid=2 */
    a.exclude_hv     = 1;
    a.disabled       = 1;
    a.wakeup_events  = 64;
    a.mmap           = 1;   /* also capture mmap events so we see new libs */

    long fd = sys_perf_event_open(&a, 0, -1, -1, 0);
    if (fd < 0) {
        LOG_ERROR("sprof", "perf_event_open failed: %s (errno=%d)",
                  strerror(errno), errno);
        fclose(g.out); g.out = NULL;
        return -1;
    }
    g.fd = (int)fd;

    size_t page = (size_t)sysconf(_SC_PAGESIZE);
    g.mmap_size = page * (1 + RING_DATA_PAGES);
    g.data_size = page * RING_DATA_PAGES;
    g.mmap_base = mmap(NULL, g.mmap_size, PROT_READ | PROT_WRITE,
                       MAP_SHARED, g.fd, 0);
    if (g.mmap_base == MAP_FAILED) {
        LOG_ERROR("sprof", "mmap failed: %s", strerror(errno));
        close(g.fd); g.fd = -1;
        fclose(g.out); g.out = NULL;
        return -1;
    }

    ioctl(g.fd, PERF_EVENT_IOC_RESET, 0);
    ioctl(g.fd, PERF_EVENT_IOC_ENABLE, 0);

    if (pthread_create(&g.reader, NULL, reader_main, NULL) != 0) {
        LOG_ERROR("sprof", "pthread_create failed");
        ioctl(g.fd, PERF_EVENT_IOC_DISABLE, 0);
        munmap(g.mmap_base, g.mmap_size);
        close(g.fd); g.fd = -1;
        fclose(g.out); g.out = NULL;
        return -1;
    }

    LOG_INFO("sprof", "started: %d Hz, ring=%zu KB, out=%s",
             SAMPLE_HZ, g.data_size / 1024, SAMPLING_PROFILER_OUT_PATH);
    return 0;
}

void sampling_profiler_shutdown(void) {
    if (g.fd < 0) return;
    g.stop = 1;
    pthread_join(g.reader, NULL);
    ioctl(g.fd, PERF_EVENT_IOC_DISABLE, 0);
    if (g.mmap_base && g.mmap_base != MAP_FAILED) {
        munmap(g.mmap_base, g.mmap_size);
        g.mmap_base = NULL;
    }
    close(g.fd);
    g.fd = -1;
    pthread_mutex_lock(&g.out_mtx);
    if (g.out) {
        uint8_t end_tag = 'E';
        fwrite(&end_tag, 1, 1, g.out);
        fclose(g.out);
        g.out = NULL;
    }
    pthread_mutex_unlock(&g.out_mtx);
}

/* Auto-start on shim load. Auto-stop on process exit via destructor. */
__attribute__((constructor))
static void sprof_ctor(void) { sampling_profiler_init(); }

__attribute__((destructor))
static void sprof_dtor(void) { sampling_profiler_shutdown(); }
