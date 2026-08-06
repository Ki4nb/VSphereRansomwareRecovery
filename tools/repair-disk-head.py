#!/usr/bin/env python3
"""Rebuild the destroyed head of a disk from the spare copies at its end.

WRITES. Dry-run by default; add --commit.

Three structures die in the first megabytes and all three have a spare that
lives past the damage:

  protective MBR + primary GPT  <- the backup GPT in the last sector
  NTFS boot sector (per volume) <- the backup VBR in the last sector of the volume

It refuses to write unless the backup GPT's entry-array CRC verifies, and it
saves every byte it is about to overwrite into <backup-dir> first. The regions
it touches are ~17 KiB of ciphertext that is already unrecoverable, so there is
nothing of value to lose - but a wrong offset would be a different story, hence
the dry run and the saved originals.

    python3 repair-disk-head.py <image> <backup-dir> [--commit]

Note this is not always needed. tools/ntfs_extract.py reads through the *backup*
boot sector and never touches the disk, so if all you want is the data, skip
this. Repair the head only when you intend to boot the guest or attach the
volume to Windows.
"""

import os
import struct
import sys
import uuid
import zlib

NTFS_TYPES = {
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7",
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac",
}


def main(argv):
    commit = "--commit" in argv
    args = [a for a in argv if not a.startswith("--")]
    if len(args) < 2:
        print(__doc__.strip())
        return 2
    path, backup_dir = args[0], args[1]

    size = os.path.getsize(path)
    pad = size % 512                 # appended ephemeral keys
    data_size = size - pad
    nsect = data_size // 512
    last = nsect - 1
    tag = os.path.basename(path).replace(" ", "_")

    print("disk  : %s" % os.path.basename(path))
    print("size  : %d bytes + %d key byte(s) = %d sectors" % (data_size, pad, nsect))
    print("mode  : %s" % ("COMMIT" if commit else "dry run"))

    fh = open(path, "r+b" if commit else "rb")

    fh.seek(data_size - 512)
    head = fh.read(512)
    if head[:8] != b"EFI PART":
        print("no backup GPT in the last sector - refusing to touch anything.")
        print("The disk may be MBR-partitioned, or expanded in VMware with the")
        print("secondary table stranded mid-device: see tools/find-backup-gpt.sh")
        return 1
    hsz = struct.unpack("<I", head[12:16])[0]
    first_usable, last_usable = struct.unpack("<QQ", head[40:56])
    diskguid = head[56:72]
    ptlba, nument, entsz = struct.unpack("<QII", head[72:88])
    arr_off = data_size - 512 - nument * entsz
    fh.seek(arr_off)
    arr = fh.read(nument * entsz)
    acrc = zlib.crc32(arr) & 0xFFFFFFFF
    if acrc != struct.unpack("<I", head[88:92])[0]:
        print("backup GPT entry-array CRC does not verify - refusing.")
        return 1
    print("backup GPT verified: %d entries" % nument)

    mbr = bytearray(512)
    mbr[446] = 0x00
    mbr[447:450] = b"\x00\x02\x00"
    mbr[450] = 0xEE                                  # protective
    mbr[451:454] = b"\xff\xff\xff"
    mbr[454:458] = struct.pack("<I", 1)
    mbr[458:462] = struct.pack("<I", min(last, 0xFFFFFFFF))
    mbr[510:512] = b"\x55\xaa"

    gpt = bytearray(512)
    gpt[0:8] = b"EFI PART"
    gpt[8:12] = struct.pack("<I", 0x00010000)
    gpt[12:16] = struct.pack("<I", hsz)
    gpt[16:20] = b"\x00\x00\x00\x00"                 # CRC computed over zeroes here
    gpt[24:32] = struct.pack("<Q", 1)                # this header is now LBA 1
    gpt[32:40] = struct.pack("<Q", last)             # alternate is the backup
    gpt[40:48] = struct.pack("<Q", first_usable)
    gpt[48:56] = struct.pack("<Q", last_usable)
    gpt[56:72] = diskguid
    gpt[72:80] = struct.pack("<Q", 2)                # entries follow at LBA 2
    gpt[80:84] = struct.pack("<I", nument)
    gpt[84:88] = struct.pack("<I", entsz)
    gpt[88:92] = struct.pack("<I", acrc)
    gpt[16:20] = struct.pack("<I", zlib.crc32(bytes(gpt[:hsz])) & 0xFFFFFFFF)

    vbrs = []
    for i in range(nument):
        ent = arr[i * entsz:(i + 1) * entsz]
        tguid = str(uuid.UUID(bytes_le=ent[:16]))
        if tguid == "00000000-0000-0000-0000-000000000000":
            continue
        first, plast = struct.unpack("<QQ", ent[32:48])
        name = ent[56:128].decode("utf-16-le").split("\x00")[0]
        poff, pend = first * 512, (plast + 1) * 512
        if tguid not in NTFS_TYPES:
            print("  part %d %-28s not NTFS, skipped" % (i + 1, name[:28]))
            continue
        fh.seek(poff)
        cur = fh.read(512)
        fh.seek(pend - 512)
        back = fh.read(512)
        if cur[3:11] == b"NTFS    ":
            print("  part %d %-28s boot sector already valid" % (i + 1, name[:28]))
            continue
        if back[3:11] != b"NTFS    ":
            print("  part %d %-28s no backup boot sector - skipped" % (i + 1, name[:28]))
            continue
        print("  part %d %-28s restore boot sector: %d <- %d"
              % (i + 1, name[:28], poff, pend - 512))
        vbrs.append((poff, back, cur))

    if not commit:
        print("dry run - nothing written. Re-run with --commit.")
        return 0

    os.makedirs(backup_dir, exist_ok=True)
    fh.seek(0)
    original = fh.read(34 * 512)
    open(os.path.join(backup_dir, tag + ".orig-lba0-33.bin"), "wb").write(original)
    for poff, _, old in vbrs:
        open(os.path.join(backup_dir, "%s.orig-vbr-%d.bin" % (tag, poff)), "wb").write(old)
    print("saved %d bytes of the original head and %d boot sector(s)"
          % (len(original), len(vbrs)))

    fh.seek(0)
    fh.write(bytes(mbr))
    fh.seek(512)
    fh.write(bytes(gpt))
    fh.seek(1024)
    fh.write(arr)
    for poff, back, _ in vbrs:
        fh.seek(poff)
        fh.write(back)
    fh.flush()
    os.fsync(fh.fileno())

    fh.seek(512)
    check = fh.read(512)
    body = bytearray(check[:hsz])
    body[16:20] = b"\x00\x00\x00\x00"
    ok = (check[:8] == b"EFI PART"
          and struct.unpack("<I", check[16:20])[0] == zlib.crc32(bytes(body)) & 0xFFFFFFFF)
    fh.seek(1024)
    ok = ok and (zlib.crc32(fh.read(nument * entsz)) & 0xFFFFFFFF) == acrc
    fh.seek(0)
    m = fh.read(512)
    ok = ok and m[450] == 0xEE and m[510:512] == b"\x55\xaa"
    for poff, back, _ in vbrs:
        fh.seek(poff)
        ok = ok and fh.read(512) == back
    print("VERIFIED" if ok else "*** VERIFICATION FAILED - do not boot this disk ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
