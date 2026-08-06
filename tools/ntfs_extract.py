#!/usr/bin/env python3
"""Pull files off an NTFS volume that will not mount.

This is the tool that actually recovers a Windows guest. It does not repair the
volume and does not need it to be mountable - not by Windows, not by ntfs-3g.

Why that matters: after this ransomware, the root directory's index block sits
in the destroyed head. Windows then reports the volume as RAW and ntfs-3g
refuses with `Corrupt index block signature: vcn 0 inode 5`. Both are refusing
over the *directory index*, not over the file data. But NTFS stores every
file's parent in its own $FILE_NAME attribute, so full paths can be rebuilt
from the MFT alone, and $MFT normally starts around 3 GiB into the volume -
far past any early-stop encryptor.

So: walk the MFT, rebuild the tree from parent references, follow each file's
data runs, write the bytes out. No index, no mount, no repair.

Read-only with respect to the damaged disk. Writes only into <outdir>.

    python3 ntfs_extract.py <image> <part-off> <part-end> <damage> <outdir> \
                            [--list] [path-prefix ...]

<part-off>/<part-end> are byte offsets of the partition (tools/ntfs_triage.py
prints them). Pass 0 and the file size when pointing at a partition device.
Prefixes are matched case-insensitively against backslash paths, e.g.
"\\Database\\Datafile\\". With none given, everything is extracted.

Files whose data overlapped the destroyed head are still written, zero-filled
where unreadable, and listed at the end. A database file in that list is
damaged; ignoring the warning gets you a corrupt restore that looks fine.
"""

import os
import struct
import sys

CHUNK = 8 << 20


def fixup(rec, bps):
    if rec[:4] != b"FILE":
        return None
    off, count = struct.unpack("<HH", rec[4:8])
    usn = rec[off:off + 2]
    out = bytearray(rec)
    for i in range(1, count):
        src = rec[off + i * 2:off + i * 2 + 2]
        end = i * bps - 2
        if end + 2 > len(out) or bytes(out[end:end + 2]) != usn:
            return None
        out[end:end + 2] = src
    return bytes(out)


def decode_runs(attr, off):
    out = []
    lcn = 0
    i = off
    while i < len(attr) and attr[i]:
        head = attr[i]
        nlen, olen = head & 0xF, head >> 4
        i += 1
        if nlen == 0 or i + nlen + olen > len(attr):
            break
        count = int.from_bytes(attr[i:i + nlen], "little")
        i += nlen
        if olen:
            lcn += int.from_bytes(attr[i:i + olen], "little", signed=True)
            i += olen
            out.append((lcn, count))
        else:
            out.append((None, count))
    return out


def attrs(rec):
    off = struct.unpack("<H", rec[20:22])[0]
    while off + 8 <= len(rec):
        atype = struct.unpack("<I", rec[off:off + 4])[0]
        if atype == 0xFFFFFFFF:
            break
        length = struct.unpack("<I", rec[off + 4:off + 8])[0]
        if length == 0 or off + length > len(rec):
            break
        yield atype, rec[off:off + length]
        off += length


class Extractor(object):
    def __init__(self, path, poff, pend, damage):
        self.fh = open(path, "rb", buffering=0)
        self.poff = poff
        self.fh.seek(poff)
        boot = self.fh.read(512)
        if boot[3:11] != b"NTFS    ":
            # Primary boot sector destroyed. The backup in the last sector of
            # the volume is a byte copy of it and lives past the damage. Using
            # it keeps this read-only - we never repair the disk to read it.
            self.fh.seek(pend - 512)
            boot = self.fh.read(512)
            if boot[3:11] != b"NTFS    ":
                raise ValueError("no usable boot sector, primary or backup")
            sys.stderr.write("primary boot sector destroyed; using the backup at %d\n"
                             % (pend - 512))
        self.bps = struct.unpack("<H", boot[11:13])[0]
        self.cl = self.bps * boot[13]
        mft_lcn = struct.unpack("<Q", boot[48:56])[0]
        cpr = struct.unpack("<b", boot[64:65])[0]
        self.recsz = (1 << -cpr) if cpr < 0 else cpr * self.cl
        self.dead_cl = (max(0, damage - poff) + self.cl - 1) // self.cl

        self.fh.seek(poff + mft_lcn * self.cl)
        rec = fixup(self.fh.read(self.recsz), self.bps)
        if not rec:
            raise ValueError("$MFT record 0 unreadable - the MFT did not survive")
        self.mft_runs = None
        for atype, attr in attrs(rec):
            if atype == 0x80 and attr[8] == 1:
                self.mft_runs = decode_runs(attr, struct.unpack("<H", attr[32:34])[0])
        if not self.mft_runs:
            raise ValueError("$MFT has no non-resident $DATA")
        self.count = sum(c for _, c in self.mft_runs) * self.cl // self.recsz
        sys.stderr.write("cluster=%d record=%d records=%d dead clusters 0..%d\n"
                         % (self.cl, self.recsz, self.count, self.dead_cl - 1))

    def read_mft(self, off, want):
        out = b""
        pos = 0
        for lcn, count in self.mft_runs:
            span = count * self.cl
            if pos + span > off and lcn is not None:
                skip = max(0, off - pos)
                self.fh.seek(self.poff + lcn * self.cl + skip)
                out += self.fh.read(min(span - skip, want - len(out)))
                if len(out) >= want:
                    break
            pos += span
        return out

    def record(self, num):
        return fixup(self.read_mft(num * self.recsz, self.recsz), self.bps)

    def stream(self, num):
        """Return (size, runs, resident_bytes, compressed) for the unnamed $DATA.

        Large or badly fragmented files spill their run list into extra MFT
        records listed by $ATTRIBUTE_LIST. Missing that truncates the file
        silently, which on a database is worse than failing outright.
        """
        rec = self.record(num)
        if not rec:
            return None
        records = [rec]
        for atype, attr in attrs(rec):
            if atype != 0x20:
                continue
            if attr[8] == 1:
                body = b""
                for lcn, count in decode_runs(attr, struct.unpack("<H", attr[32:34])[0]):
                    if lcn is None:
                        continue
                    self.fh.seek(self.poff + lcn * self.cl)
                    body += self.fh.read(count * self.cl)
                body = body[:struct.unpack("<Q", attr[48:56])[0]]
            else:
                off = struct.unpack("<H", attr[20:22])[0]
                body = attr[off:off + struct.unpack("<I", attr[16:20])[0]]
            off = 0
            seen = set()
            while off + 26 <= len(body):
                atype2 = struct.unpack("<I", body[off:off + 4])[0]
                rlen = struct.unpack("<H", body[off + 4:off + 6])[0]
                if rlen == 0:
                    break
                ref = struct.unpack("<Q", body[off + 16:off + 24])[0] & 0xFFFFFFFFFFFF
                if atype2 == 0x80 and body[off + 6] == 0 and ref != num and ref not in seen:
                    seen.add(ref)
                    ext = self.record(ref)
                    if ext:
                        records.append(ext)
                off += rlen

        parts, size, resident, comp = [], 0, None, False
        for r in records:
            for atype, attr in attrs(r):
                if atype != 0x80 or attr[9]:        # unnamed $DATA only
                    continue
                if attr[8] == 0:
                    off = struct.unpack("<H", attr[20:22])[0]
                    length = struct.unpack("<I", attr[16:20])[0]
                    resident = attr[off:off + length]
                    size = max(size, length)
                else:
                    real = struct.unpack("<Q", attr[48:56])[0]
                    if real:
                        size = max(size, real)
                    if struct.unpack("<H", attr[34:36])[0]:
                        comp = True
                    parts.append((struct.unpack("<Q", attr[16:24])[0],
                                  decode_runs(attr, struct.unpack("<H", attr[32:34])[0])))
        parts.sort(key=lambda x: x[0])
        return size, [r for _, rl in parts for r in rl], resident, comp

    def write(self, dest, size, runs, resident):
        """Write the file out. Returns bytes that came from the destroyed head."""
        tmp = dest + ".part"
        dead = 0
        with open(tmp, "wb") as out:
            if resident is not None:
                out.write(resident[:size])
            else:
                written = 0
                for lcn, count in runs:
                    if written >= size:
                        break
                    want = min(count * self.cl, size - written)
                    if lcn is None:
                        left = want
                        while left > 0:
                            n = min(left, CHUNK)
                            out.write(b"\0" * n)
                            left -= n
                    else:
                        overlap = max(0, min(lcn + count, self.dead_cl) - lcn)
                        if overlap > 0:
                            dead += overlap * self.cl
                        self.fh.seek(self.poff + lcn * self.cl)
                        left = want
                        while left > 0:
                            n = min(left, CHUNK)
                            buf = self.fh.read(n)
                            if len(buf) < n:
                                buf += b"\0" * (n - len(buf))
                            out.write(buf)
                            left -= n
                    written += want
        os.replace(tmp, dest)
        return dead

    def walk(self):
        names, kinds = {}, {}
        i = 0
        while i < self.count:
            blob = self.read_mft(i * self.recsz, 512 * self.recsz)
            if not blob:
                break
            for j in range(len(blob) // self.recsz):
                rec = fixup(blob[j * self.recsz:(j + 1) * self.recsz], self.bps)
                if not rec:
                    continue
                flags = struct.unpack("<H", rec[22:24])[0]
                if not flags & 1:                    # deleted
                    continue
                name = parent = None
                for atype, attr in attrs(rec):
                    if atype == 0x30:
                        c = struct.unpack("<H", attr[20:22])[0]
                        pref = struct.unpack("<Q", attr[c:c + 8])[0] & 0xFFFFFFFFFFFF
                        nlen, nspace = attr[c + 64], attr[c + 65]
                        cand = attr[c + 66:c + 66 + nlen * 2].decode("utf-16-le", "replace")
                        if name is None or nspace != 2:
                            name, parent = cand, pref
                if name:
                    names[i + j] = (name, parent)
                    kinds[i + j] = bool(flags & 2)
            i += 512
        return names, kinds


def full_path(names, num, depth=0):
    if num == 5:
        return ""
    if num not in names or depth > 60:
        return None
    name, parent = names[num]
    up = full_path(names, parent, depth + 1)
    return None if up is None else up + "\\" + name


def main(argv):
    if len(argv) < 5:
        print(__doc__.strip())
        return 2
    path, poff, pend, damage, outdir = argv[0], int(argv[1]), int(argv[2]), \
        int(argv[3]), argv[4]
    rest = argv[5:]
    listing = "--list" in rest
    prefixes = [p.lower() for p in rest if not p.startswith("--")] or [""]

    ex = Extractor(path, poff, pend, damage)
    names, kinds = ex.walk()

    rows = []
    for num in names:
        if num < 16:                     # NTFS's own metadata files
            continue
        p = full_path(names, num)
        if not p:
            continue
        if any(p.lower().startswith(x) for x in prefixes):
            rows.append((p, num, kinds[num]))
    rows.sort()

    if not listing:
        os.makedirs(outdir, exist_ok=True)
    nfiles = ndirs = 0
    total = 0
    damaged = []
    for p, num, isdir in rows:
        target = os.path.join(outdir, p.replace("\\", "/").lstrip("/"))
        if isdir:
            ndirs += 1
            if not listing:
                os.makedirs(target, exist_ok=True)
            continue
        info = ex.stream(num)
        if not info:
            continue
        size, runs, resident, comp = info
        nfiles += 1
        total += size
        if listing:
            print("  %14d  %s%s" % (size, p, "   [COMPRESSED]" if comp else ""))
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            dead = ex.write(target, size, runs, resident)
        except OSError as exc:
            # NTFS reserves names like $Extend; some drivers refuse to create
            # them on the destination. They are NTFS bookkeeping, not your data.
            print("  FAILED %s: %s" % (p, exc))
            continue
        got = os.path.getsize(target)
        note = ""
        if got != size:
            note += "  *** SIZE %d != %d ***" % (got, size)
        if dead:
            note += "  [%d bytes from the destroyed head]" % dead
            damaged.append((p, dead, size))
        if comp:
            note += "  [COMPRESSED - stored raw, not decompressed]"
        print("  %14d  %s%s" % (size, p, note))
        sys.stdout.flush()

    print("")
    print("directories: %d   files: %d   bytes: %d (%.2f GiB)"
          % (ndirs, nfiles, total, total / 1024.0 ** 3))
    if damaged:
        print("")
        print("%d file(s) overlapped the destroyed head - these are NOT intact:" % len(damaged))
        for p, dead, size in sorted(damaged, key=lambda x: -x[1]):
            print("   %10.2f MiB damaged of %10.2f MiB   %s"
                  % (dead / 1048576.0, size / 1048576.0, p))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
