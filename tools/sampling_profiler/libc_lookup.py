#!/usr/bin/env python3
import sys

# parse "addr name" lines
syms = []
with open('/tmp/move_libc_syms.txt') as f:
    for ln in f:
        parts = ln.split()
        if len(parts) >= 2:
            try:
                syms.append((int(parts[0], 16), parts[1]))
            except ValueError:
                pass
syms.sort()

import bisect
addrs = [a for a,_ in syms]

# offsets to look up
hot = [
    (1461, 0xe768c),
    (637,  0xd9cb4),
    (326,  0xb57cc),
    (324,  0xdf5a8),
    (284,  0xdf3e0),
    (41,   0x9aec8),
    (33,   0x9b540),
    (8,    0x2b1f0),
    (8,    0x2b2cc),
]
for count, off in hot:
    i = bisect.bisect_right(addrs, off) - 1
    if i < 0:
        print(f"  0x{off:x} → ???")
        continue
    a, n = syms[i]
    print(f"  {count:4d} samples  0x{off:5x} → {n}+0x{off-a:x}")
