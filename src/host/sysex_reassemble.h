#ifndef SCHWUNG_SYSEX_REASSEMBLE_H
#define SCHWUNG_SYSEX_REASSEMBLE_H

/*
 * USB-MIDI SysEx reassembler.
 *
 * MIDI arrives as 4-byte USB-MIDI packets; a SysEx (F0..F7) is spread across
 * several with CIN 0x4 (start/continue) and CIN 0x5/0x6/0x7 (end with 1/2/3
 * data bytes). This accumulates the data bytes into one whole message so a
 * caller can hand it to JS as a single blob (callGlobalFunctionN).
 *
 * The logic is lifted verbatim from the standalone host's proven inline path
 * (schwung_host.c) so the overtake path (process_shadow_midi in shadow_ui.c,
 * where ion actually runs) can share it instead of the current 3-byte-fragment
 * delivery that a SysEx dump can't survive. Single stream, one producer — same
 * assumption the inline version already relied on.
 */

#include <stdint.h>

#define SYSEX_REASSEMBLE_MAX 1024

typedef struct {
    unsigned char buf[SYSEX_REASSEMBLE_MAX];
    int len;
    int active;      /* a message is being accumulated */
    int overflowed;  /* it overran the buffer: keep owning its trailing packets
                      * so the terminator is dropped, not leaked as channel-voice */
} sysex_reassemble_t;

/* Return codes from sysex_reassemble_feed(). */
enum {
    SYSEX_FEED_NOT_SYSEX  = -1,  /* not SysEx reassembly — caller handles normally */
    SYSEX_FEED_INCOMPLETE =  0,  /* consumed into an in-progress message (or dropped) */
    /* > 0 : a complete F0..F7 message of that length now sits in r->buf */
};

static inline void sysex_reassemble_init(sysex_reassemble_t *r) {
    r->len = 0;
    r->active = 0;
    r->overflowed = 0;
}

/* Feed one USB-MIDI packet: `cin` (low nibble of byte 0) plus its three data
 * bytes p1..p3. See the enum for the return contract; on a positive return the
 * whole message is in r->buf with that length. An overrun drops the message
 * (returns SYSEX_FEED_INCOMPLETE) rather than deliver a truncated one. */
int sysex_reassemble_feed(sysex_reassemble_t *r, uint8_t cin,
                          uint8_t p1, uint8_t p2, uint8_t p3);

#endif /* SCHWUNG_SYSEX_REASSEMBLE_H */
