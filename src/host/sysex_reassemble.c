#include "host/sysex_reassemble.h"

int sysex_reassemble_feed(sysex_reassemble_t *r, uint8_t cin,
                          uint8_t p1, uint8_t p2, uint8_t p3) {
    const uint8_t data[3] = { p1, p2, p3 };

    if (cin == 0x4) {
        /* start (F0 ...) or continue */
        if (p1 == 0xF0) { r->len = 0; r->active = 1; }
        if (r->active) {
            /* Overrun on an untrusted stream: abandon rather than run past
             * the buffer. */
            if (r->len + 3 > SYSEX_REASSEMBLE_MAX) {
                r->active = 0; r->len = 0;
            } else {
                for (int b = 0; b < 3; b++) r->buf[r->len++] = data[b];
            }
        }
        return SYSEX_FEED_INCOMPLETE;
    }

    if (cin >= 0x5 && cin <= 0x7) {
        /* 0x5/0x6/0x7 = sysex end with 1/2/3 data bytes. A real terminator's
         * LAST byte is 0xF7; if it isn't (e.g. a lone CIN 0x5 carrying a
         * system-common 0xF6 Tune Request), this is NOT our terminator — tell
         * the caller to handle it normally and keep any reassembly intact. */
        int nbytes = cin - 0x4;                 /* 0x5->1, 0x6->2, 0x7->3 */
        if (r->active && data[nbytes - 1] == 0xF7) {
            if (r->len + nbytes > SYSEX_REASSEMBLE_MAX) {
                /* Would overrun — drop, don't deliver a truncated (unterminated)
                 * message. */
                r->active = 0; r->len = 0;
                return SYSEX_FEED_INCOMPLETE;
            }
            for (int b = 0; b < nbytes; b++) r->buf[r->len++] = data[b];
            int complete_len = r->len;
            r->active = 0; r->len = 0;
            return complete_len;                /* > 0: whole F0..F7 in r->buf */
        }
        return SYSEX_FEED_NOT_SYSEX;
    }

    return SYSEX_FEED_NOT_SYSEX;                 /* channel-voice etc. */
}
