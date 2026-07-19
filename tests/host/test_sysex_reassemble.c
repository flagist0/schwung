/*
 * Unit test for the USB-MIDI SysEx reassembler. Proves the logic the overtake
 * path (process_shadow_midi) needs so ion receives a whole SysEx dump instead
 * of 3-byte fragments — plus every edge case surfaced by review-crew on the
 * untrusted external-MIDI stream (channel-voice interleave, real-time
 * interleave, single-packet SysEx, overrun tail, restart, lone terminator).
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "host/sysex_reassemble.h"

static void fail(const char *msg) {
    fprintf(stderr, "FAIL: %s\n", msg);
    exit(1);
}

int main(void) {
    sysex_reassemble_t r;

    /* 1. Two-packet message F0 7D 01 02 F7 (5 bytes): 0x4 start then 0x6 end. */
    sysex_reassemble_init(&r);
    if (sysex_reassemble_feed(&r, 0x4, 0xF0, 0x7D, 0x01) != SYSEX_FEED_INCOMPLETE)
        fail("start chunk should be incomplete");
    if (sysex_reassemble_feed(&r, 0x6, 0x02, 0xF7, 0x00) != 5) fail("end chunk should complete 5 bytes");
    const uint8_t want1[5] = { 0xF0, 0x7D, 0x01, 0x02, 0xF7 };
    if (memcmp(r.buf, want1, 5) != 0) fail("reassembled bytes wrong");

    /* 2. Single-byte terminator CIN 0x5 (=0xF7) after a 0x4 start: F0 7D 11 F7. */
    sysex_reassemble_init(&r);
    sysex_reassemble_feed(&r, 0x4, 0xF0, 0x7D, 0x11);
    if (sysex_reassemble_feed(&r, 0x5, 0xF7, 0x00, 0x00) != 4) fail("0x5 terminator should complete 4 bytes");
    const uint8_t want2[4] = { 0xF0, 0x7D, 0x11, 0xF7 };
    if (memcmp(r.buf, want2, 4) != 0) fail("short reassembled bytes wrong");

    /* 3. Single-PACKET complete SysEx (no preceding 0x4): F0 F7 via CIN 0x6. */
    sysex_reassemble_init(&r);
    if (sysex_reassemble_feed(&r, 0x6, 0xF0, 0xF7, 0x00) != 2) fail("F0 F7 single packet should complete");
    if (r.buf[0] != 0xF0 || r.buf[1] != 0xF7) fail("single-packet F0F7 bytes wrong");
    /*    and F0 7D F7 via CIN 0x7. */
    sysex_reassemble_init(&r);
    if (sysex_reassemble_feed(&r, 0x7, 0xF0, 0x7D, 0xF7) != 3) fail("F0 xx F7 single packet should complete");

    /* 4. Channel-voice packet MID-reassembly abandons the stale partial, and a
     *    later terminator does NOT complete a spliced message. */
    sysex_reassemble_init(&r);
    sysex_reassemble_feed(&r, 0x4, 0xF0, 0x7D, 0x01);
    if (sysex_reassemble_feed(&r, 0x9, 0x90, 0x3C, 0x64) != SYSEX_FEED_NOT_SYSEX)
        fail("note-on should be not-sysex");
    if (r.active != 0) fail("channel-voice must abandon the in-flight SysEx");
    if (sysex_reassemble_feed(&r, 0x5, 0xF7, 0x00, 0x00) != SYSEX_FEED_NOT_SYSEX)
        fail("orphan terminator after abandon must not complete a spliced message");

    /* 5. System REAL-TIME (0xF8 clock) mid-reassembly must NOT abandon; the
     *    message still completes without the clock byte spliced in. */
    sysex_reassemble_init(&r);
    sysex_reassemble_feed(&r, 0x4, 0xF0, 0x7D, 0x01);
    if (sysex_reassemble_feed(&r, 0xF, 0xF8, 0x00, 0x00) != SYSEX_FEED_NOT_SYSEX)
        fail("realtime clock should be not-sysex");
    if (r.active != 1) fail("realtime must NOT abandon an in-flight SysEx");
    if (sysex_reassemble_feed(&r, 0x6, 0x02, 0xF7, 0x00) != 5) fail("message should still complete after realtime");
    if (memcmp(r.buf, want1, 5) != 0) fail("realtime byte must not be spliced into the message");

    /* 6. Overrun: a >MAX message is dropped, and its terminator is SWALLOWED
     *    (not leaked to the caller as fake channel-voice). */
    sysex_reassemble_init(&r);
    sysex_reassemble_feed(&r, 0x4, 0xF0, 0x00, 0x00);
    for (int i = 0; i < (SYSEX_REASSEMBLE_MAX / 3) + 4; i++)
        sysex_reassemble_feed(&r, 0x4, 0x01, 0x02, 0x03);
    if (r.overflowed != 1) fail("overrun should set overflowed, keeping ownership of the tail");
    if (sysex_reassemble_feed(&r, 0x6, 0x04, 0xF7, 0x00) != SYSEX_FEED_INCOMPLETE)
        fail("overrun terminator must be swallowed as incomplete, not delivered");
    if (r.active != 0) fail("overrun message must reset after its terminator");

    /* 7. A new F0 while a previous message is still active restarts cleanly. */
    sysex_reassemble_init(&r);
    sysex_reassemble_feed(&r, 0x4, 0xF0, 0x11, 0x22);          /* message A, never terminated */
    sysex_reassemble_feed(&r, 0x4, 0xF0, 0x33, 0x44);          /* message B restarts */
    if (sysex_reassemble_feed(&r, 0x5, 0xF7, 0x00, 0x00) != 4) fail("restart should complete B as 4 bytes");
    const uint8_t wantB[4] = { 0xF0, 0x33, 0x44, 0xF7 };
    if (memcmp(r.buf, wantB, 4) != 0) fail("restart must discard A and keep only B");

    /* 8. Channel-voice with no active message: plain not-sysex passthrough. */
    sysex_reassemble_init(&r);
    if (sysex_reassemble_feed(&r, 0x9, 0x90, 0x3C, 0x64) != SYSEX_FEED_NOT_SYSEX)
        fail("note-on with no active msg must be not-sysex");

    /* 9. Lone terminator (F7) with no active message and no F0: not-sysex. */
    sysex_reassemble_init(&r);
    if (sysex_reassemble_feed(&r, 0x5, 0xF7, 0x00, 0x00) != SYSEX_FEED_NOT_SYSEX)
        fail("lone F7 terminator with no active message must be not-sysex");

    /* 10. A lone CIN 0x5 system-common (0xF6, not a terminator): not-sysex. */
    sysex_reassemble_init(&r);
    if (sysex_reassemble_feed(&r, 0x5, 0xF6, 0x00, 0x00) != SYSEX_FEED_NOT_SYSEX)
        fail("non-F7 single-byte common must be not-sysex");

    /* 11. Orphan continue (0x4 with no preceding F0) is swallowed, not leaked. */
    sysex_reassemble_init(&r);
    if (sysex_reassemble_feed(&r, 0x4, 0x11, 0x22, 0x33) != SYSEX_FEED_INCOMPLETE)
        fail("orphan continue should be consumed as incomplete");
    if (r.len != 0) fail("orphan continue must not accumulate");

    /* 12. Boundary: a message EXACTLY SYSEX_REASSEMBLE_MAX bytes completes, not
     *     dropped (guards the '> MAX' vs '>= MAX' off-by-one). Build F0 + fill
     *     + F7 totalling exactly MAX via 0x4 chunks then a 0x5 terminator. */
    sysex_reassemble_init(&r);
    sysex_reassemble_feed(&r, 0x4, 0xF0, 0xAA, 0xAA);         /* len 3 */
    while (r.len < SYSEX_REASSEMBLE_MAX - 1)                   /* leave 1 for the F7 */
        sysex_reassemble_feed(&r, 0x4, 0xAA, 0xAA, 0xAA);     /* +3 each; MAX-1 divisible plan below */
    if (r.len != SYSEX_REASSEMBLE_MAX - 1) fail("boundary fill must land exactly one short of MAX");
    if (sysex_reassemble_feed(&r, 0x5, 0xF7, 0x00, 0x00) != SYSEX_REASSEMBLE_MAX)
        fail("message exactly MAX bytes must complete, not drop");

    printf("test_sysex_reassemble: all assertions passed\n");
    return 0;
}
