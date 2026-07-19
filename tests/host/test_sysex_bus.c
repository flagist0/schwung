/*
 * Step 0 — internal SysEx bus transport proof (no hardware, no QuickJS).
 *
 * Models three bus participants and drives a Property-Exchange-shaped
 * round-trip entirely in-process:
 *
 *   ion (Initiator, midi_ci)  --ping F0 7D 01 2A F7-->  sampler slot (Responder)
 *   sampler slot              --pong F0 7D 02 2A F7-->  ion
 *   dumb synth slot (no midi_ci) sees NEITHER (SysEx is gated on capability)
 *
 * Asserts the three things the current 3-byte channel-voice path can't do:
 *   1. a whole 5-byte SysEx reaches the slot uncut (past the 3-byte limit),
 *   2. the slot's reply reaches ion (the missing slot->host return wire),
 *   3. a non-midi_ci slot is never handed the SysEx (manifest capability gate),
 *   4. the emitter is never handed its own message (from_idx skip).
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "host/sysex_bus.h"

static void fail(const char *msg) {
    fprintf(stderr, "FAIL: %s\n", msg);
    exit(1);
}

/* Educational-use manufacturer id 0x7D; byte[2] = command (01 ping / 02 pong),
 * byte[3] = an arbitrary payload byte that a 3-byte truncation would drop. */
static const uint8_t PING[] = { 0xF0, 0x7D, 0x01, 0x2A, 0xF7 };
static const uint8_t PONG[] = { 0xF0, 0x7D, 0x02, 0x2A, 0xF7 };

/* Shared bus (filled in main). */
static sysex_bus_participant_t g_bus[3];
static const int ION = 0, SAMPLER = 1, DUMB = 2;

/* --- ion: the Initiator. Captures whatever it receives. --- */
static uint8_t ion_rx[64];
static int     ion_rx_len = 0;
static void ion_deliver(void *ctx, const uint8_t *bytes, int len) {
    (void)ctx;
    if (len > (int)sizeof(ion_rx)) fail("ion rx overflow");
    memcpy(ion_rx, bytes, len);
    ion_rx_len = len;
}

/* --- sampler slot: the Responder. On a ping, replies with a pong. --- */
static int sampler_rx_len = 0;   /* length the slot actually received (proves no truncation) */
static void sampler_deliver(void *ctx, const uint8_t *bytes, int len) {
    (void)ctx;
    sampler_rx_len = len;
    /* Recognise a ping: F0 7D 01 ... — respond in-process, re-entrantly. */
    if (len >= 3 && bytes[0] == 0xF0 && bytes[1] == 0x7D && bytes[2] == 0x01) {
        sysex_bus_emit(g_bus, 3, SAMPLER, PONG, (int)sizeof(PONG));
    }
}

/* --- dumb synth slot: no midi_ci capability. Must never see a SysEx. --- */
static int dumb_rx_count = 0;
static void dumb_deliver(void *ctx, const uint8_t *bytes, int len) {
    (void)ctx; (void)bytes; (void)len;
    dumb_rx_count++;
}

int main(void) {
    g_bus[ION]     = (sysex_bus_participant_t){ NULL, ion_deliver,     1 };
    g_bus[SAMPLER] = (sysex_bus_participant_t){ NULL, sampler_deliver, 1 };
    g_bus[DUMB]    = (sysex_bus_participant_t){ NULL, dumb_deliver,    0 };

    /* ion emits the ping onto the bus. */
    sysex_bus_emit(g_bus, 3, ION, PING, (int)sizeof(PING));

    /* 1. the slot received the WHOLE ping — 5 bytes, not 3. */
    if (sampler_rx_len != (int)sizeof(PING)) fail("ping was truncated before reaching the slot");

    /* 2. the pong made it back to ion, whole. */
    if (ion_rx_len != (int)sizeof(PONG)) fail("pong did not reach ion intact");
    if (memcmp(ion_rx, PONG, sizeof(PONG)) != 0) fail("ion received wrong bytes for the pong");

    /* 3. the non-midi_ci slot saw neither SysEx (capability gate). */
    if (dumb_rx_count != 0) fail("SysEx leaked to a slot that did not declare midi_ci");

    /* 4. from_idx skip: ion never received its own ping (only the pong). */
    if (ion_rx[2] != 0x02) fail("ion was handed its own ping (from_idx skip failed)");

    printf("test_sysex_bus: all assertions passed\n");
    return 0;
}
