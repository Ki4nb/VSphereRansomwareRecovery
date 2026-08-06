#!/usr/bin/env python3
"""Restore the NTFS metadata files that died in the first megabytes.

WRITES. Dry-run by default; add --commit.

NTFS keeps twelve metadata files at the very start of the volume, so an
early-stop encryptor takes several of them. They split cleanly into two groups:

  invariant - identical on every NTFS volume of the same Windows version, so an
    intact volume can donate them byte for byte:
      $UpCase   131072 bytes, the Unicode uppercase table
      $AttrDef    2560 bytes, the attribute definitions
      $Boot      sectors 1-15, the generic bootstrap (sector 0 is per-volume
                 and must be restored from the volume's own backup instead)

  volume-specific - cannot be donated:
      $MFTMirr  but it is only a copy of the first records of $MFT, so it can be
                rebuilt from this volume's own intact $MFT
      $Secure, $Extend/$TxfLog, and the root directory index - only chkdsk
                rebuilds these

The donor is usually already on the same disk. A Windows install puts its
recovery partition near the *end* of the disk, hundreds of gigabytes past any
early-stop damage, and it is the same NTFS version as C:.

Honest warning: doing this does not necessarily make Windows mount the volume.
The root directory index (inode 5) is volume-specific and is the structure
Windows is actually refusing over - it reports RAW, and ntfs-3g says
`Corrupt index block signature: vcn 0 inode 5`. If your goal is the data rather
than a bootable guest, use tools/ntfs_extract.py instead: it needs no index and
no mount. See docs/windows-recovery.md.

    python3 repair-ntfs-meta.py <donor-image> <donor-part-off> <backup-dir> \
                                <target-image> <target-part-off> [more pairs] [--commit]
"""

import os
import struct
import sys


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


def open_volume(path, poff, mode="rb"):
    fh = open(path, mode)
    fh.seek(poff)
    boot = fh.read(512)
    if boot[3:11] != b"NTFS    ":
        raise ValueError("no NTFS boot sector at %s:%d - restore it first with "
                         "tools/repair-disk-head.py" % (os.path.basename(path), poff))
    bps = struct.unpack("<H", boot[11:13])[0]
    cl = bps * boot[13]
    mft = struct.unpack("<Q", boot[48:56])[0]
    cpr = struct.unpack("<b", boot[64:65])[0]
    recsz = (1 << -cpr) if cpr < 0 else cpr * cl
    return {"fh": fh, "path": path, "poff": poff, "bps": bps, "cl": cl,
            "mft": mft, "recsz": recsz}


def data_extents(vol, num):
    """(size, [(abs_offset, length)]) for the unnamed $DATA of record num."""
    vol["fh"].seek(vol["poff"] + vol["mft"] * vol["cl"] + num * vol["recsz"])
    rec = fixup(vol["fh"].read(vol["recsz"]), vol["bps"])
    if not rec:
        raise ValueError("record %d unreadable in %s" % (num, vol["path"]))
    for atype, attr in attrs(rec):
        if atype != 0x80 or attr[9]:
            continue
        if attr[8] == 0:
            return struct.unpack("<I", attr[16:20])[0], None
        size = struct.unpack("<Q", attr[48:56])[0]
        ext = []
        for lcn, count in decode_runs(attr, struct.unpack("<H", attr[32:34])[0]):
            if lcn is not None:
                ext.append((vol["poff"] + lcn * vol["cl"], count * vol["cl"]))
        return size, ext
    raise ValueError("no unnamed $DATA in record %d of %s" % (num, vol["path"]))


def read_extents(vol, ext, size):
    out = b""
    for off, length in ext:
        vol["fh"].seek(off)
        out += vol["fh"].read(length)
        if len(out) >= size:
            break
    return out[:size]


def write_extents(vol, ext, data, skip=0):
    pos = wrote = 0
    for off, length in ext:
        start, end = pos, pos + length
        pos = end
        lo, hi = max(start, skip), min(end, len(data))
        if hi <= lo:
            continue
        vol["fh"].seek(off + (lo - start))
        vol["fh"].write(data[lo:hi])
        wrote += hi - lo
    return wrote


def validate_upcase(table):
    """A real $UpCase maps a-z to A-Z and leaves A-Z and 0-9 alone.

    Cheap, but it is the difference between donating a genuine table and
    donating 128 KiB of whatever happened to be at that offset.
    """
    if len(table) != 131072:
        return False
    vals = struct.unpack("<65536H", table)
    return (all(vals[c] == c - 32 for c in range(0x61, 0x7B))
            and all(vals[c] == c for c in range(0x41, 0x5B))
            and all(vals[c] == c for c in range(0x30, 0x3A)))


def main(argv):
    commit = "--commit" in argv
    args = [a for a in argv if not a.startswith("--")]
    if len(args) < 5 or (len(args) - 3) % 2:
        print(__doc__.strip())
        return 2
    donor_path, donor_off, backup_dir = args[0], int(args[1]), args[2]
    targets = [(args[i], int(args[i + 1])) for i in range(3, len(args), 2)]

    donor = open_volume(donor_path, donor_off)
    up_size, up_ext = data_extents(donor, 10)
    ad_size, ad_ext = data_extents(donor, 4)
    bt_size, bt_ext = data_extents(donor, 7)
    upcase = read_extents(donor, up_ext, up_size)
    attrdef = read_extents(donor, ad_ext, ad_size)
    boot = read_extents(donor, bt_ext, min(bt_size, 8192))

    print("donor: %s @ %d" % (os.path.basename(donor_path), donor_off))
    print("  $UpCase %d  $AttrDef %d  $Boot %d" % (len(upcase), len(attrdef), len(boot)))
    if not validate_upcase(upcase):
        print("  donor $UpCase is not a canonical uppercase table - refusing.")
        return 1
    if len(attrdef) != 2560 or attrdef[:8] != "$STANDA".encode("utf-16-le")[:8]:
        print("  donor $AttrDef does not look like the standard table - refusing.")
        return 1
    if len(boot) < 8192:
        print("  donor $Boot is short - refusing.")
        return 1
    print("  donor validated")
    print("  mode: %s\n" % ("COMMIT" if commit else "dry run"))

    if commit:
        os.makedirs(backup_dir, exist_ok=True)

    rc = 0
    for tpath, toff in targets:
        vol = open_volume(tpath, toff, "r+b" if commit else "rb")
        tag = "%s.vol%d" % (os.path.basename(tpath).replace(" ", "_"), toff)
        print("target: %s @ %d (cluster %d, record %d)"
              % (os.path.basename(tpath), toff, vol["cl"], vol["recsz"]))

        mm_size, mm_ext = data_extents(vol, 1)
        _, up_t = data_extents(vol, 10)
        ad_dst_size, ad_t = data_extents(vol, 4)
        _, bt_t = data_extents(vol, 7)
        _, mft_ext = data_extents(vol, 0)
        mirror = read_extents(vol, mft_ext, mm_size)   # $MFTMirr mirrors $MFT

        pad = max(0, min(ad_dst_size, 4096) - len(attrdef))
        jobs = [("$MFTMirr", mm_ext, mirror, 0),
                ("$UpCase", up_t, upcase, 0),
                ("$AttrDef", ad_t, attrdef + b"\x00" * pad, 0),
                ("$Boot", bt_t, boot[:8192], 512)]    # keep this volume's sector 0

        for name, ext, data, skip in jobs:
            print("  %-9s %d byte(s) into %d extent(s)%s"
                  % (name, len(data) - skip, len(ext),
                     "  [keeps sector 0]" if skip else ""))
        if not commit:
            print("")
            continue

        for name, ext, data, skip in jobs:
            avail = sum(l for _, l in ext)
            old = read_extents(vol, ext, min(len(data), avail))
            open(os.path.join(backup_dir, "%s.%s.orig.bin" % (tag, name.strip("$"))),
                 "wb").write(old)
        for name, ext, data, skip in jobs:
            print("    wrote %-9s %d bytes" % (name, write_extents(vol, ext, data, skip)))
        vol["fh"].flush()
        os.fsync(vol["fh"].fileno())
        vol["fh"].close()

        check = open_volume(tpath, toff)
        ok = True
        for name, num, expect in (("$UpCase", 10, upcase), ("$AttrDef", 4, attrdef)):
            _, ext = data_extents(check, num)
            good = read_extents(check, ext, len(expect)) == expect
            ok &= good
            print("    verify %-9s %s" % (name, "OK" if good else "*** MISMATCH ***"))
        _, ext = data_extents(check, 1)
        good = read_extents(check, ext, mm_size) == mirror
        ok &= good
        print("    verify %-9s %s" % ("$MFTMirr", "OK" if good else "*** MISMATCH ***"))
        _, ext = data_extents(check, 7)
        got = read_extents(check, ext, 8192)
        print("    verify %-9s sector 0 %s, sectors 1-15 %s"
              % ("$Boot",
                 "preserved" if got[3:11] == b"NTFS    " else "*** LOST ***",
                 "OK" if got[512:8192] == boot[512:8192] else "*** MISMATCH ***"))
        print("  -> %s\n" % ("all verified" if ok else "PROBLEM - do not boot this volume"))
        if not ok:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
