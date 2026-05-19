/*
 * sampling_profiler.h — in-process sampling profiler for the schwung shim.
 *
 * Uses perf_event_open(pid=0) to sample the calling thread's instruction
 * pointer at ~1 kHz with kernel-side callchain capture. Works without
 * root because we self-profile our own process (perf_event_paranoid=2
 * is satisfied by exclude_kernel=1).
 *
 * Motivation: MoveOriginal has Linux capabilities set on the binary
 * (cap_ipc_lock,cap_sys_nice,cap_sys_resource=ep) which deny ptrace
 * from any same-UID process. strace, perf record -p, gdb all fail.
 * But the shim runs inside MoveOriginal via LD_PRELOAD, so it CAN
 * call perf_event_open on its own thread and read /proc/self/maps
 * for ASLR symbolication. See docs/REALTIME_SAFETY.md note.
 *
 * Trigger: presence of /data/UserData/schwung/profile_on at the time
 * the constructor runs (LD_PRELOAD load). No effect otherwise.
 *
 * Output: /data/UserData/schwung/profile.bin (binary). Use
 * tools/sampling_profiler/parse_profile.py to symbolicate.
 */

#ifndef SCHWUNG_SAMPLING_PROFILER_H
#define SCHWUNG_SAMPLING_PROFILER_H

#define SAMPLING_PROFILER_FLAG_PATH "/data/UserData/schwung/profile_on"
#define SAMPLING_PROFILER_OUT_PATH  "/data/UserData/schwung/profile.bin"

/* Returns 0 on success, non-zero if profiling could not be started
 * (flag file absent, syscall failed, mmap failed, etc.). Logs to
 * the unified log either way. Safe to call from constructor; the
 * background reader thread is spawned only on success. */
int sampling_profiler_init(void);

/* Stop profiling and close the output file. Idempotent. Normally
 * called from a destructor, but the kernel cleans up cleanly on
 * process exit so this is best-effort. */
void sampling_profiler_shutdown(void);

#endif /* SCHWUNG_SAMPLING_PROFILER_H */
