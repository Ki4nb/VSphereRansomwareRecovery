#!/usr/bin/env python3
"""Read an NTFS disk that the encryptor hit, and say exactly what died.

Windows has one word for every kind of NTFS damage: RAW. That is useless
mid-incident, because a volume whose boot sector is gone and a volume whose
every file is gone look identical from Disk Management. This prints the
difference.

For each NTFS partition it reports:

  * whether the boot sector survived, and whether the backup copy at the end of
    the volume is usable (it almost always is - it lives past the damage)
  * whether $MFT survived, which is the question that decides everything. On a
    default NTFS format $MFT starts around 3 GiB into the volume, far past any
    early-stop encryptor, so the file table normally lives.
  * which of the twelve NTFS metadata files were inside the damage
  * every ordinary file with clusters inside the damage, largest first

Read-only. Never writes, never repairs. Run it on the ESXi host against a flat
file, or in a rescue VM against a device.

    python3 ntfs_triage.py <image-or-device> <damage-boundary-bytes> [--files]

The boundary comes from tools/measure-boundary.py. Do not guess it.
"""

import os
import struct
import sys
import uuid

SYS_FILES = ["$MFT", "$MFTMirr", "$LogFile", "$Volume", "$AttrDef", ". (root dir)",
             "$Bitmap", "$Boot", "$BadClus", "$Secure", "$UpCase", "$Extend"]

# Only the two that carry file data. ESP and MSR have nothing to read.
NTFS_TYPES = {
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7": "Basic data (NTFS)",
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac": "Windows Recovery",
}
ALL_TYPES = dict(NTFS_TYPES)
ALL_TYPES.update({
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "EFI System",
    "e3c9e316-0b5c-4db8-817d-f92df00215ae": "Microsoft Reserved",
    "0fc63daf-8483-4772-8e79-3d69d8477de4": "Linux filesystem",
    "e6d6d379-f507-44c2-a23c-238f2a3df928": "Linux LVM",
})


def fixup(rec, bps):
    """Undo NTFS's update-sequence array.

    The last two bytes of every sector in an MFT record are replaced by a
    counter; the real values live in the fixup array. Skip this and every
    record that spans two sectors decodes as garbage in its final field.
    """
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
            out.append((None, count))          # sparse
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


class Volume(object):
    def __init__(self, fh, poff, pend, damage):
        self.fh, self.poff, self.pend = fh, poff, pend
        self.vbr_source = None
        fh.seek(poff)
        boot = fh.read(512)
        if boot[3:11] != b"NTFS    ":
            fh.seek(pend - 512)
            boot = fh.read(512)
            if boot[3:11] != b"NTFS    ":
                raise ValueError("no usable boot sector, primary or backup")
            self.vbr_source = "backup (primary destroyed)"
        else:
            self.vbr_source = "primary"
        self.bps = struct.unpack("<H", boot[11:13])[0]
        self.cl = self.bps * boot[13]
        self.total = struct.unpack("<Q", boot[40:48])[0]
        self.mft_lcn = struct.unpack("<Q", boot[48:56])[0]
        self.mftmirr_lcn = struct.unpack("<Q", boot[56:64])[0]
        cpr = struct.unpack("<b", boot[64:65])[0]
        self.recsz = (1 << -cpr) if cpr < 0 else cpr * self.cl
        self.serial = struct.unpack("<Q", boot[72:80])[0]
        dead_bytes = max(0, min(damage, pend) - poff)
        self.dead_cl = (dead_bytes + self.cl - 1) // self.cl
        self.mft_runs = None

    def record(self, num):
        if self.mft_runs is None:
            self.fh.seek(self.poff + self.mft_lcn * self.cl)
            rec = fixup(self.fh.read(self.recsz), self.bps)
            if not rec:
                raise ValueError("$MFT record 0 is unreadable - the MFT did not survive")
            for atype, attr in attrs(rec):
                if atype == 0x80 and attr[8] == 1:
                    self.mft_runs = decode_runs(attr, struct.unpack("<H", attr[32:34])[0])
        want = num * self.recsz
        pos = 0
        for lcn, count in self.mft_runs:
            span = count * self.cl
            if pos + span > want and lcn is not None:
                self.fh.seek(self.poff + lcn * self.cl + (want - pos))
                return fixup(self.fh.read(self.recsz), self.bps)
            pos += span
        return None

    def mft_records(self):
        if self.mft_runs is None:
            self.record(0)
        return sum(c for _, c in self.mft_runs) * self.cl // self.recsz

    def damage_of(self, rec):
        """Bytes of this record's data that fell inside the destroyed head."""
        lost = 0
        size = 0
        for atype, attr in attrs(rec):
            if atype in (0x80, 0xA0) and attr[8] == 1:
                if atype == 0x80:
                    size = max(size, struct.unpack("<Q", attr[48:56])[0])
                for lcn, count in decode_runs(attr, struct.unpack("<H", attr[32:34])[0]):
                    if lcn is None:
                        continue
                    overlap = max(0, min(lcn + count, self.dead_cl) - lcn)
                    if overlap > 0:
                        lost += overlap * self.cl
        return lost, size


def partitions(fh, size):
    """Read the partition table from the backup GPT at the end of the disk.

    The primary table at LBA 1 is always inside the damage. The backup is a
    full copy in the last sector and normally verifies.
    """
    data_size = size - (size % 512)
    fh.seek(data_size - 512)
    head = fh.read(512)
    if head[:8] != b"EFI PART":
        return []
    ptlba, count, entsz = struct.unpack("<QII", head[72:88])
    fh.seek(data_size - 512 - count * entsz)
    arr = fh.read(count * entsz)
    out = []
    for i in range(count):
        ent = arr[i * entsz:(i + 1) * entsz]
        tguid = str(uuid.UUID(bytes_le=ent[:16]))
        if tguid == "00000000-0000-0000-0000-000000000000":
            continue
        first, last = struct.unpack("<QQ", ent[32:48])
        name = ent[56:128].decode("utf-16-le").split("\x00")[0]
        out.append((i + 1, tguid, name, first * 512, (last + 1) * 512))
    return out


def full_path(names, num, depth=0):
    if num == 5:
        return ""
    if num not in names or depth > 60:
        return None
    name, parent = names[num]
    up = full_path(names, parent, depth + 1)
    return None if up is None else up + "\\" + name


def report_files(vol):
    total = vol.mft_records()
    names, damaged = {}, []
    i = 0
    while i < total:
        chunk = []
        for n in range(i, min(i + 256, total)):
            rec = vol.record(n)
            chunk.append((n, rec))
        for num, rec in chunk:
            if not rec or not (struct.unpack("<H", rec[22:24])[0] & 1):
                continue
            name = parent = None
            for atype, attr in attrs(rec):
                if atype == 0x30:
                    c = struct.unpack("<H", attr[20:22])[0]
                    pref = struct.unpack("<Q", attr[c:c + 8])[0] & 0xFFFFFFFFFFFF
                    nlen, nspace = attr[c + 64], attr[c + 65]
                    cand = attr[c + 66:c + 66 + nlen * 2].decode("utf-16-le", "replace")
                    if name is None or nspace != 2:      # prefer the long name
                        name, parent = cand, pref
            if name:
                names[num] = (name, parent)
            lost, size = vol.damage_of(rec)
            if lost:
                damaged.append((num, lost, size))
        i += 256

    print("  -- ordinary files with data inside the destroyed head: %d --" % len(damaged))
    damaged.sort(key=lambda x: -x[1])
    for num, lost, size in damaged:
        path = full_path(names, num) or "?<%d>" % num
        print("     %9.2f MiB lost of %9.2f MiB   %s"
              % (lost / 1048576.0, size / 1048576.0, path))


def main(argv):
    if len(argv) < 2:
        print(__doc__.strip())
        return 2
    path, damage = argv[0], int(argv[1])
    list_files = "--files" in argv
    size = os.path.getsize(path)
    fh = open(path, "rb")

    print("%s" % os.path.basename(path))
    print("  size %d, damage boundary %d (0x%X)\n" % (size, damage, damage))

    parts = partitions(fh, size)
    if not parts:
        print("  no backup GPT at the end of the disk.")
        print("  Either this is MBR-partitioned, or it was expanded in VMware and the")
        print("  secondary table is stranded mid-device - see tools/find-backup-gpt.sh")
        return 1

    for num, tguid, name, poff, pend in parts:
        label = ALL_TYPES.get(tguid, tguid)
        print("  [%d] %-20s %-28s bytes %d..%d (%.2f GiB)"
              % (num, label, name[:28], poff, pend, (pend - poff) / 1024.0 ** 3))
        if poff >= damage:
            print("      starts past the boundary - completely untouched")
        else:
            lost = min(pend, damage) - poff
            print("      first %d bytes (%.0f MiB) of this partition are destroyed"
                  % (lost, lost / 1048576.0))
        if tguid not in NTFS_TYPES:
            print("")
            continue
        try:
            vol = Volume(fh, poff, pend, damage)
        except ValueError as exc:
            print("      %s\n" % exc)
            continue
        print("      boot sector : %s  (cluster %d, record %d, serial %016x)"
              % (vol.vbr_source, vol.cl, vol.recsz, vol.serial))
        mft_abs = poff + vol.mft_lcn * vol.cl
        print("      $MFT at LCN %d -> byte %d : %s"
              % (vol.mft_lcn, mft_abs,
                 "SURVIVED" if mft_abs >= damage else "*** DESTROYED ***"))
        try:
            print("      %d MFT records" % vol.mft_records())
        except ValueError as exc:
            print("      %s\n" % exc)
            continue

        print("      -- NTFS metadata files --")
        for num2 in range(12):
            rec = vol.record(num2)
            if not rec:
                print("        %-14s record unreadable" % SYS_FILES[num2])
                continue
            lost, _ = vol.damage_of(rec)
            note = ""
            if lost and SYS_FILES[num2] in ("$UpCase", "$AttrDef"):
                note = "  (invariant - donate from an intact volume)"
            elif lost and SYS_FILES[num2] == "$MFTMirr":
                note = "  (rebuild from $MFT)"
            elif lost and SYS_FILES[num2] == ". (root dir)":
                note = "  (this is what makes Windows say RAW)"
            print("        %-14s %s%s"
                  % (SYS_FILES[num2], "DESTROYED" if lost else "intact", note))
        if list_files:
            report_files(vol)
        print("")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
