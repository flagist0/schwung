#include "host/sysex_reassemble.h"

int sysex_reassemble_feed(sysex_reassemble_t *r, uint8_t cin,
                          uint8_t p1, uint8_t p2, uint8_t p3) {
    const uint8_t data[3] = { p1, p2, p3 };

    if (cin == 0x4) {
        /* start (F0 ...) or continue */
        if (p1 == 0xF0) { r->len = 0; r->active = 1; r->overflowed = 0; }
        if (r->active) {
            /* Overrun on an untrusted stream: stop appending but STAY active so
             * the message keeps owning its trailing packets — its terminator is
             * then dropped (below) rather than leaking as fake channel-voice. */
            if (r->overflowed || r->len + 3 > SYSEX_REASSEMBLE_MAX) {
                r->overflowed = 1;
            } else {
                for (int b = 0; b < 3; b++) r->buf[r->len++] = data[b];
            }
        }
        return SYSEX_FEED_INCOMPLETE;
    }

    if (cin >= 0x5 && cin <= 0x7) {
        /* 0x5/0x6/0x7 = sysex end with 1/2/3 data bytes; a real terminator's
         * LAST byte is 0xF7. If it isn't (e.g. a lone CIN 0x5 carrying a
         * system-common 0xF6 Tune Request), this is NOT our terminator — fall
         * through to the abandon-guard and let the caller handle it. */
        int nbytes = cin - 0x4;                 /* 0x5->1, 0x6->2, 0x7->3 */
        /* A complete SysEx that fits in a single packet (F0..F7 via CIN 0x6/0x7)
         * has no preceding CIN 0x4 start — recognise it here. Gated on the
         * terminator too, so a malformed lone F0 can't half-open a message. */
        if (!r->active && p1 == 0xF0 && data[nbytes - 1] == 0xF7) {
            r->len = 0; r->active = 1; r->overflowed = 0;
        }
        if (r->active && data[nbytes - 1] == 0xF7) {
            if (r->overflowed || r->len + nbytes > SYSEX_REASSEMBLE_MAX) {
                /* Dropped (overran) — never deliver a truncated message, and
                 * swallow the terminator so it isn't misread as channel-voice. */
                r->active = 0; r->overflowed = 0; r->len = 0;
                return SYSEX_FEED_INCOMPLETE;
            }
            for (int b = 0; b < nbytes; b++) r->buf[r->len++] = data[b];
            int complete_len = r->len;
            r->active = 0; r->overflowed = 0; r->len = 0;
            return complete_len;                /* > 0: whole F0..F7 in r->buf */
        }
        /* not our terminator — fall through to the abandon-guard below */
    }

    /* Any other packet (channel-voice, or a non-terminator "end" CIN). A
     * channel-voice STATUS byte (0x80..0xEF) arriving mid-reassembly means the
     * SysEx was abandoned (dropped end packet / unplug) — drop the stale partial
     * so a later stray byte can't be misread as its terminator or spliced onto
     * an unrelated burst. System real-time (0xF8 clock etc., status 0xF0..0xFF)
     * may LEGALLY interleave inside a SysEx, so it must NOT abandon. (From the
     * original inline guard in schwung_host.c.) */
    if (r->active && (p1 & 0x80) && (p1 & 0xF0) != 0xF0) {
        r->active = 0; r->overflowed = 0; r->len = 0;
    }
    return SYSEX_FEED_NOT_SYSEX;
}
