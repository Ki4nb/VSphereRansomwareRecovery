#!/usr/bin/env python3
"""
Locate filesystem structures on a partition whose start was destroyed.

Read-only. Scans for the signatures that survive past a ransomware-damaged
head, and — for ext2/3/4 — computes where the filesystem actually begins from
the superblock's own block-group number.

Usage:
    python3 find_fs.py /dev/sda1 [scan_GiB]      # default 8 GiB
"""
import struct
import sys

dev = sys.argv[1] if len(sys.argv) > 1 else "/dev/sda1"
limit = int(float(sys.argv[2] if len(sys.argv) > 2 else 8) * (1 << 30))
CH = 32 << 20
OVERLAP = 1 << 16

SIGS = [
    (b"XFSB",           0, "XFS superblock"),
    (b"_BHRfS_M",       0, "btrfs superblock"),
    (b"LUKS\xba\xbe",   0, "LUKS header (guest-encrypted)"),
    (b"LABELONE",       0, "LVM2 PV label"),
    (b"SWAPSPACE2",     0, "swap"),
    (b"\x53\xef",    0x38, "ext2/3/4 superblock"),
]

found_ext = []
seen = set()

with open(dev, "rb") as f:
    base = 0
    prev = b""
    while base < limit:
        buf = f.read(CH)
        if not buf:
            break
        data = prev + buf
        dbase = base - len(prev)

        for magic, moff, name in SIGS:
            pos = data.find(magic)
            while pos != -1:
                sb = pos - moff
                abs_off = dbase + sb
                if sb >= 0 and sb + 0x60 <= len(data) and abs_off % 1024 == 0:
                    if magic == b"\x53\xef":
                        logbs = struct.unpack_from("<I", data, sb + 0x18)[0]
                        bpg = struct.unpack_from("<I", data, sb + 0x20)[0]
                        gnr = struct.unpack_from("<H", data, sb + 0x5A)[0]
                        if logbs <= 6 and bpg and bpg <= 1 << 20:
                            bs = 1024 << logbs
                            start = abs_off - gnr * bpg * bs
                            key = (start, bs)
                            print(f"  ext4 sb @0x{abs_off:x}  block={bs}  "
                                  f"blocks/group={bpg}  group={gnr}  "
                                  f"-> fs starts at 0x{start:x} ({start} bytes)")
                            if key not in seen and gnr:
                                seen.add(key)
                                found_ext.append((start, bs, bpg, gnr, abs_off))
                    elif abs_off not in seen:
                        seen.add(abs_off)
                        print(f"  {name} @0x{abs_off:x}")
                pos = data.find(magic, pos + 1)

        prev = buf[-OVERLAP:]
        base += len(buf)

print()
if not found_ext:
    print("No ext4 superblock found in the scanned range.")
    print("Try a larger range, or the partition may hold LVM/XFS/btrfs/LUKS.")
else:
    start, bs, bpg, gnr, abs_off = found_ext[0]
    blk = abs_off // bs
    print("=" * 62)
    print(f"filesystem starts at byte {start} (0x{start:x}) within {dev}")
    print(f"block size {bs}, first usable backup superblock at block {blk}")
    print()
    print("Check it (read-only):")
    if start == 0:
        print(f"    e2fsck -n -b {blk} -B {bs} {dev}")
        print(f"    mount -o ro,sb={blk * bs // 1024} {dev} /mnt/root")
    else:
        print(f"    losetup -r -o {start} -f --show {dev}      # -> /dev/loopN")
        print(f"    e2fsck -n -b {blk - start // bs} -B {bs} /dev/loopN")
        print(f"    mount -o ro,sb={(blk - start // bs) * bs // 1024} /dev/loopN /mnt/root")
    print("=" * 62)
