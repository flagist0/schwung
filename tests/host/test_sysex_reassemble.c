/*
 * Unit test for the extracted USB-MIDI SysEx reassembler. Proves the logic the
 * overtake path (process_shadow_midi) needs so ion receives a whole SysEx dump
 * instead of 3-byte fragments — the gap behind S1's pending whole-loop HW test.
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

    /* 1. Two-packet message F0 7D 01 02 F7 (5 bytes): a 0x4 start-chunk then a
     *    0x6 end-chunk (2 data bytes, terminator 0xF7). */
    sysex_reassemble_init(&r);
    if (sysex_reassemble_feed(&r, 0x4, 0xF0, 0x7D, 0x01) != SYSEX_FEED_INCOMPLETE)
        fail("start chunk should be incomplete");
    int n = sysex_reassemble_feed(&r, 0x6, 0x02, 0xF7, 0x00);
    if (n != 5) fail("end chunk should complete a 5-byte message");
    const uint8_t want[5] = { 0xF0, 0x7D, 0x01, 0x02, 0xF7 };
    if (memcmp(r.buf, want, 5) != 0) fail("reassembled bytes wrong");

    /* 2. Single-packet message via CIN 0x7 (F0 + end-with-3): F0 7D F7. */
    sysex_reassemble_init(&r);
    if (sysex_reassemble_feed(&r, 0x4, 0xF0, 0x7D, 0x11) != SYSEX_FEED_INCOMPLETE)
        fail("start of short msg incomplete");
    n = sysex_reassemble_feed(&r, 0x5, 0xF7, 0x00, 0x00);   /* end with 1 byte = 0xF7 */
    if (n != 4) fail("single-byte terminator should complete 4-byte message");
    const uint8_t want2[4] = { 0xF0, 0x7D, 0x11, 0xF7 };
    if (memcmp(r.buf, want2, 4) != 0) fail("short reassembled bytes wrong");

    /* 3. A long message that overruns SYSEX_REASSEMBLE_MAX is DROPPED, not
     *    delivered truncated. Feed 0x4 continue-chunks past the cap, then a
     *    terminator — must NOT report completion. */
    sysex_reassemble_init(&r);
    sysex_reassemble_feed(&r, 0x4, 0xF0, 0x00, 0x00);       /* start */
    for (int i = 0; i < (SYSEX_REASSEMBLE_MAX / 3) + 4; i++)
        sysex_reassemble_feed(&r, 0x4, 0x01, 0x02, 0x03);   /* overrun */
    n = sysex_reassemble_feed(&r, 0x6, 0x04, 0xF7, 0x00);
    if (n > 0) fail("overrun message must not be reported complete");

    /* 4. Channel-voice packet is not SysEx: caller handles it normally. */
    sysex_reassemble_init(&r);
    if (sysex_reassemble_feed(&r, 0x9, 0x90, 0x3C, 0x64) != SYSEX_FEED_NOT_SYSEX)
        fail("note-on must be reported not-sysex");

    /* 5. A CIN 0x5 that is NOT a 0xF7 terminator (e.g. system-common) while no
     *    message is active falls through to the normal path, untouched. */
    sysex_reassemble_init(&r);
    if (sysex_reassemble_feed(&r, 0x5, 0xF6, 0x00, 0x00) != SYSEX_FEED_NOT_SYSEX)
        fail("lone non-F7 single-byte common must be not-sysex");

    /* 6. A stray continue with no preceding F0 start is swallowed, not leaked. */
    sysex_reassemble_init(&r);
    if (sysex_reassemble_feed(&r, 0x4, 0x11, 0x22, 0x33) != SYSEX_FEED_INCOMPLETE)
        fail("orphan continue should be consumed as incomplete");
    if (r.len != 0) fail("orphan continue must not accumulate");

    printf("test_sysex_reassemble: all assertions passed\n");
    return 0;
}
