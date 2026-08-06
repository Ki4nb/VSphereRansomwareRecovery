#!/usr/bin/env python3
"""Measure where the encryptor stopped, instead of assuming it.

The first incident documented in this repository stopped at 0x20000000
(512 MiB). A later one, same actor and byte-identical run.sh, stopped at
0x20800000 (520 MiB). Nothing on the host tells you which build you have, and
assuming the smaller number silently reports 8 MiB of real damage as intact
plaintext - which is the direction that loses data.

So measure it. Encrypted output is uniform random: high Shannon entropy and
almost no zero bytes. Real filesystem data is structured, sparse, or both. The
transition between the two is the boundary, and it is sharp to the sector.

Read-only. Run it on the ESXi host against a flat file, or in a rescue VM
against a device.

    python3 measure-boundary.py <image-or-device> [more ...]

Prints the boundary per file and warns if they disagree, which would mean two
different builds ran on the same host.
"""

import math
import os
import struct
import sys

# Where we have seen it stop. Not a whitelist - just labels for the report.
KNOWN = {
    0x20000000: "512 MiB - the originally documented Babuk-derived build",
    0x20800000: "520 MiB - the August 2026 build (same run.sh, different binary)",
}

COARSE_STEP = 4 << 20      # 4 MiB while hunting
COARSE_LIMIT = 2 << 30     # give up past 2 GiB; nothing seen stops that late
SAMPLE = 4096


def entropy(buf):
    if not buf:
        return 0.0
    counts = [0] * 256
    for byte in buf:
        counts[byte] += 1
    total = len(buf)
    out = 0.0
    for c in counts:
        if c:
            p = c / total
            out -= p * math.log(p, 2)
    return out


def looks_encrypted(fh, off, size=SAMPLE):
    """Uniform random: entropy near 8 bits/byte and essentially no zero bytes.

    Compressed file content can also be high entropy, which is why the caller
    refines with a run of consecutive samples rather than trusting one hit.
    """
    fh.seek(off)
    buf = fh.read(size)
    if len(buf) < size:
        return None
    return entropy(buf) > 7.5 and buf.count(0) / len(buf) < 0.02


def measure(path):
    size = os.path.getsize(path)
    fh = open(path, "rb")

    print("%s" % os.path.basename(path))
    print("  size        : %d bytes (%.2f GiB)" % (size, size / 1024.0 ** 3))
    rem = size % 512
    passes = rem // 32 if rem in (32, 64) else None
    print("  size %% 512   : %d%s" % (
        rem,
        "" if passes is None else "  -> %d encryption pass(es)" % passes))
    # NB: do NOT short-circuit on rem == 0. On a VMFS flat file that means
    # "renamed but never encrypted", but a block device is always 512-aligned,
    # and so is an image whose appended keys were stripped. Measure regardless
    # and let the head itself say whether anything was encrypted.

    last = -1
    off = 0
    while off + SAMPLE <= min(size, COARSE_LIMIT):
        hit = looks_encrypted(fh, off)
        if hit is None:
            break
        if hit:
            last = off
        elif last >= 0 and off > last + (16 << 20):
            break                      # 16 MiB of clean data - we are past it
        off += COARSE_STEP
    if last < 0:
        if rem == 0:
            print("  no encrypted region at the head, and size %% 512 == 0:")
            print("  NOT ENCRYPTED. If it carries the ransomware extension it was")
            print("  renamed only, and needs a descriptor and nothing else.")
        else:
            print("  no high-entropy region found at the head - is this really damaged?")
        return None

    # Refine to the sector. Walk forward from the last coarse hit and take the
    # last 512-byte sample that still looks like ciphertext.
    best = last
    off = last
    stop = min(size, last + COARSE_STEP + SAMPLE)
    while off + 512 <= stop:
        if looks_encrypted(fh, off, 512):
            best = off
        elif off > best + 65536:
            break
        off += 512

    boundary = best + 512
    print("  last encrypted sector starts %d (0x%X)" % (best, best))
    print("  BOUNDARY    : %d bytes = 0x%X = %.2f MiB"
          % (boundary, boundary, boundary / 1048576.0))
    print("  matches     : %s" % KNOWN.get(boundary, "*** not a build seen before ***"))
    fh.close()
    return boundary


def main(argv):
    if not argv:
        print(__doc__.strip())
        return 2
    seen = {}
    for path in argv:
        try:
            b = measure(path)
        except (OSError, ValueError) as exc:
            print("%s: %s" % (path, exc))
            continue
        if b:
            seen.setdefault(b, []).append(os.path.basename(path))
        print("")

    if len(seen) > 1:
        print("WARNING: more than one boundary on this host:")
        for b, files in sorted(seen.items()):
            print("  0x%X (%.2f MiB): %d file(s)" % (b, b / 1048576.0, len(files)))
        print("Treat each disk with its own constant. Do not average them.")
        return 1
    if seen:
        b = list(seen)[0]
        print("Use %d (0x%X) as the damage boundary for this host." % (b, b))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
