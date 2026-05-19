#!/usr/bin/env python3
"""Parse SPROF binary dumps from schwung sampling_profiler.c.

Outputs:
  - Top N hottest PCs (by sample count), with library + file-offset
  - Top N hottest call-stacks (folded), suitable for flamegraph.pl
"""
import struct
import sys
import bisect
from collections import Counter


def parse_maps(text):
    """Parse /proc/self/maps content into [(start, end, offset, path), ...]
    sorted by start. Only x permission lines kept (executable code)."""
    out = []
    for line in text.split('\n'):
        if not line.strip():
            continue
        # 7f001000-7f001234 r-xp 00000000 fd:00 1234567 /path/to/lib.so
        parts = line.split(None, 5)
        if len(parts) < 5:
            continue
        addr_range, perms, offset, dev, inode = parts[:5]
        path = parts[5] if len(parts) >= 6 else ''
        if 'x' not in perms:
            continue
        start, end = (int(x, 16) for x in addr_range.split('-'))
        off = int(offset, 16)
        out.append((start, end, off, path))
    out.sort()
    return out


def lookup_pc(pc, maps):
    """Return (path, file_offset) for a PC, or None."""
    # bsearch by start
    i = bisect.bisect_right(maps, (pc, 1 << 63, 0, '')) - 1
    if i < 0:
        return None
    start, end, mfile_off, path = maps[i]
    if start <= pc < end:
        return (path, mfile_off + (pc - start))
    return None


def fmt_pc(pc, maps):
    r = lookup_pc(pc, maps)
    if not r:
        return f"???+0x{pc:x}"
    path, off = r
    name = path.split('/')[-1] if path else '???'
    return f"{name}+0x{off:x}"


def main(path, top_n=30):
    with open(path, 'rb') as f:
        magic = f.read(8)
        if magic != b'SPROF\0\0\0':
            print(f'bad magic: {magic!r}', file=sys.stderr); sys.exit(1)
        ver, hz = struct.unpack('<II', f.read(8))
        t0 = struct.unpack('<Q', f.read(8))[0]
        maps_len = struct.unpack('<I', f.read(4))[0]
        maps_text = f.read(maps_len).decode('utf-8', errors='replace')
        maps = parse_maps(maps_text)

        print(f'# SPROF v{ver}  hz={hz}  t0_ns={t0}  maps_len={maps_len}')
        print(f'# {len(maps)} executable mappings')

        ip_counts = Counter()
        stack_counts = Counter()
        tid_counts = Counter()
        n_samples = 0
        while True:
            tag = f.read(1)
            if not tag:
                break
            if tag == b'E':
                break
            if tag != b'S':
                print(f'# unknown tag {tag!r} at offset {f.tell()-1}, stopping',
                      file=sys.stderr)
                break
            rec = f.read(8 + 4 + 8 + 2)
            if len(rec) < 22:
                break
            ip, tid, t, nr = struct.unpack('<QIQH', rec)
            chain = struct.unpack(f'<{nr}Q', f.read(8 * nr)) if nr else ()
            ip_counts[ip] += 1
            tid_counts[tid] += 1
            # Strip first PERF_CONTEXT marker (high bits set, e.g. 0xfff...)
            useful = [pc for pc in chain if (pc >> 63) == 0 or (pc & 0xfff0000000000000) != 0xfff0000000000000]
            # Stack as ";"-joined symbol names, leaf-first
            stack_key = ';'.join(fmt_pc(pc, maps) for pc in useful[:12])
            if not stack_key:
                stack_key = fmt_pc(ip, maps)
            stack_counts[stack_key] += 1
            n_samples += 1

        print(f'# total samples: {n_samples}')
        print(f'# threads (TID: count): ',
              ', '.join(f'{tid}={cnt}' for tid, cnt in tid_counts.most_common()))
        print()
        print(f'## Top {top_n} hottest PCs (by sample count)')
        for pc, c in ip_counts.most_common(top_n):
            r = lookup_pc(pc, maps)
            label = fmt_pc(pc, maps)
            print(f'{c:6d}  {label:50s}  pc=0x{pc:x}')

        print()
        print(f'## Top {top_n} hottest stacks (leaf;...;root)')
        for stack, c in stack_counts.most_common(top_n):
            print(f'{c:6d}  {stack}')


if __name__ == '__main__':
    p = sys.argv[1] if len(sys.argv) > 1 else 'profile.bin'
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    main(p, n)
