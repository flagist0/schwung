#!/usr/bin/env python3
"""Parse SPROF binary dumps from schwung sampling_profiler.c.

Modes:
  parse_sprof.py <profile.bin> [top_n=30]
      Human-readable report: top hot PCs + top folded stacks.

  parse_sprof.py <profile.bin> --folded > out.folded
      Folded stacks suitable for FlameGraph (root;...;leaf count per line).
      Pipe into flamegraph.pl:
        ./parse_sprof.py prof.bin --folded \\
          | flamegraph.pl --title="Move main thread" > flame.svg
      flamegraph.pl from https://github.com/brendangregg/FlameGraph

If a sibling file `<profile.bin>.symbols.txt` exists, it's used as a
lookup table: lines of the form `lib offset symbol` (whitespace-
separated) get substituted into the output so symbols replace raw
offsets. Generate with the libc-symbol extraction documented in the
README; do the same for MoveOriginal once you have a stripped-ish
symbol map.
"""
import struct
import sys
import bisect
from collections import Counter
from pathlib import Path


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


def load_symbol_table(path):
    """Optional symbol lookup. File format:
       <libname> <hex offset> <symbol>
    Returns dict[(libname, sorted_offset_list)] for bsearch.
    """
    if not Path(path).exists():
        return None
    by_lib = {}
    with open(path) as f:
        for ln in f:
            parts = ln.split(None, 2)
            if len(parts) != 3:
                continue
            lib = parts[0]
            try:
                off = int(parts[1], 16)
            except ValueError:
                continue
            sym = parts[2].strip()
            by_lib.setdefault(lib, []).append((off, sym))
    for lib in by_lib:
        by_lib[lib].sort()
    return by_lib


def fmt_pc(pc, maps, sym_table=None):
    r = lookup_pc(pc, maps)
    if not r:
        return f"???+0x{pc:x}"
    path, off = r
    name = path.split('/')[-1] if path else '???'
    if sym_table and name in sym_table:
        # bsearch for largest entry <= off
        lst = sym_table[name]
        i = bisect.bisect_right(lst, (off, '')) - 1
        if i >= 0:
            sym_off, sym = lst[i]
            return f"{name}!{sym}+0x{off - sym_off:x}"
    return f"{name}+0x{off:x}"


def iter_samples(f, maps, sym_table=None, max_depth=24):
    """Yield (tid, time_ns, ip, [stack frames root-first])."""
    while True:
        tag = f.read(1)
        if not tag or tag == b'E':
            break
        if tag != b'S':
            print(f'# unknown tag {tag!r} at offset {f.tell()-1}', file=sys.stderr)
            break
        rec = f.read(8 + 4 + 8 + 2)
        if len(rec) < 22:
            break
        ip, tid, t, nr = struct.unpack('<QIQH', rec)
        chain = struct.unpack(f'<{nr}Q', f.read(8 * nr)) if nr else ()
        # Strip kernel PERF_CONTEXT markers (top bits all set, e.g. 0xfff...)
        useful = [pc for pc in chain
                  if (pc & 0xfff0000000000000) != 0xfff0000000000000]
        # FlameGraph wants root-first; perf delivers leaf-first
        frames = [fmt_pc(pc, maps, sym_table) for pc in useful[:max_depth]]
        frames.reverse()
        yield tid, t, ip, frames


def report_human(path, top_n, sym_table):
    with open(path, 'rb') as f:
        magic = f.read(8)
        if magic != b'SPROF\0\0\0':
            print(f'bad magic: {magic!r}', file=sys.stderr); sys.exit(1)
        ver, hz = struct.unpack('<II', f.read(8))
        t0 = struct.unpack('<Q', f.read(8))[0]
        maps_len = struct.unpack('<I', f.read(4))[0]
        maps = parse_maps(f.read(maps_len).decode('utf-8', errors='replace'))

        print(f'# SPROF v{ver}  hz={hz}  t0_ns={t0}  maps_len={maps_len}')
        print(f'# {len(maps)} executable mappings')

        ip_counts = Counter()
        stack_counts = Counter()
        tid_counts = Counter()
        n = 0
        for tid, _, ip, frames in iter_samples(f, maps, sym_table):
            ip_counts[ip] += 1
            tid_counts[tid] += 1
            # Stack key: ROOT-first (matches folded format), so the same
            # tail shows up consolidated in top stacks too.
            key = ';'.join(frames) or fmt_pc(ip, maps, sym_table)
            stack_counts[key] += 1
            n += 1
        print(f'# total samples: {n}')
        print('# threads (TID: count): ',
              ', '.join(f'{tid}={cnt}' for tid, cnt in tid_counts.most_common()))
        print()
        print(f'## Top {top_n} hottest PCs')
        for pc, c in ip_counts.most_common(top_n):
            print(f'{c:6d}  {fmt_pc(pc, maps, sym_table):50s}  pc=0x{pc:x}')
        print()
        print(f'## Top {top_n} hottest stacks (root;...;leaf)')
        for stack, c in stack_counts.most_common(top_n):
            print(f'{c:6d}  {stack}')


def report_folded(path, sym_table):
    """One line per unique stack: 'frame1;frame2;...;leaf count'.
    Output suitable for piping into flamegraph.pl."""
    with open(path, 'rb') as f:
        magic = f.read(8)
        if magic != b'SPROF\0\0\0':
            print(f'bad magic: {magic!r}', file=sys.stderr); sys.exit(1)
        f.read(8 + 8)  # skip ver, hz, t0
        maps_len = struct.unpack('<I', f.read(4))[0]
        maps = parse_maps(f.read(maps_len).decode('utf-8', errors='replace'))
        stacks = Counter()
        for _, _, ip, frames in iter_samples(f, maps, sym_table):
            key = ';'.join(frames) or fmt_pc(ip, maps, sym_table)
            stacks[key] += 1
    for stack, c in stacks.most_common():
        print(f'{stack} {c}')


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__); sys.exit(2)
    profile = args[0]
    sym_path = profile + '.symbols.txt'
    sym_table = load_symbol_table(sym_path)
    if sym_table:
        print(f'# loaded symbols from {sym_path}: '
              f'{sum(len(v) for v in sym_table.values())} entries '
              f'across {len(sym_table)} libs', file=sys.stderr)
    if '--folded' in args:
        report_folded(profile, sym_table)
    else:
        top_n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
        report_human(profile, top_n, sym_table)


if __name__ == '__main__':
    main()
