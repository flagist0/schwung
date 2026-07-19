#include "host/sysex_bus.h"

void sysex_bus_emit(const sysex_bus_participant_t *parts, int n,
                    int from_idx, const uint8_t *bytes, int len) {
    if (!parts || !bytes || len <= 0) return;

    const int is_sysex = (bytes[0] == 0xF0);

    for (int i = 0; i < n; i++) {
        if (i == from_idx) continue;               /* never hand a message back to its author */
        if (is_sysex && !parts[i].cap_midi_ci) continue;  /* SysEx only to MIDI-CI participants */
        if (parts[i].deliver)
            parts[i].deliver(parts[i].ctx, bytes, len);   /* WHOLE message, at its real length */
    }
}
