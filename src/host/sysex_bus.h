#ifndef SCHWUNG_SYSEX_BUS_H
#define SCHWUNG_SYSEX_BUS_H

/*
 * Internal MIDI-CI / SysEx bus (step 0 — transport proof).
 *
 * The chain-slot MIDI path today carries only 3-byte channel-voice messages
 * (shadow_chain_dispatch_midi_to_slots) and a slot's output only ever reaches
 * Move's hardware (queueInternalMidiSend -> cable 0). Property Exchange needs
 * two things that path can't do: deliver a WHOLE (variable-length) SysEx to a
 * slot, and let a slot's SysEx reply reach ion.
 *
 * This is the software fan-out that makes ion + slots (and, later, an external
 * tap) one logical bus. It is a plain in-process call — NOT a physical cable-2
 * loopback — so it sidesteps the cable-2 echo cascade entirely.
 *
 * Routing rule (deliberately dumb — the bus knows nothing about PE):
 *   - a message is handed to every participant WHOLE, at its real length;
 *   - a SysEx (bytes[0] == 0xF0) goes only to participants marked SysEx-eligible
 *     (cap_midi_ci). This gate protects NAIVE INTERNAL slots: a plain synth slot
 *     that only parses note bytes must not be handed a SysEx it might choke on,
 *     so an internal slot is eligible only if it opted in via its manifest
 *     ("midi_ci": true). Where the flag comes from differs by participant type:
 *       - internal slot  -> from its manifest (opt-in; we can't assume it self-filters);
 *       - external tap    -> intrinsically true. A real device on cable 2 is a
 *                            MIDI citizen that self-filters unknown SysEx by
 *                            manufacturer id / MUID by contract, so it needs no
 *                            declaration — the wire is SysEx-safe by definition;
 *       - ion (Initiator) -> intrinsically true.
 *     The bus rule stays uniform (SysEx -> cap_midi_ci participants); only the
 *     SOURCE of the flag differs, set at each participant's wiring — there is no
 *     per-device special case inside the bus.
 *   - the emitter is skipped (from_idx), so a message is never handed back to
 *     its own author. This is the caller's OWN index, known at the call site —
 *     not a new opaque token. Pass from_idx < 0 to deliver to everyone (self-
 *     delivery is harmless anyway: an Initiator ignores a Get, a Responder
 *     ignores a Reply — MIDI-CI role asymmetry prevents a loop).
 *
 * Addressing within the bus is by MUID, decided by the receiver — not by this
 * function. The bus only broadcasts among the participants that speak CI.
 */

#include <stdint.h>

typedef struct {
    void *ctx;                                              /* opaque receiver handle */
    void (*deliver)(void *ctx, const uint8_t *bytes, int len);
    int   cap_midi_ci;   /* manifest "midi_ci": true — may receive SysEx */
} sysex_bus_participant_t;

/* Fan `bytes` (a complete MIDI message, possibly a multi-byte SysEx) to every
 * participant except `from_idx`, honouring the capability gate above. */
void sysex_bus_emit(const sysex_bus_participant_t *parts, int n,
                    int from_idx, const uint8_t *bytes, int len);

#endif /* SCHWUNG_SYSEX_BUS_H */
