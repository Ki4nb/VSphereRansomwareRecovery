#!/usr/bin/env python3
"""
Babuk ESXi recovery - rebuild the partition/filesystem map of a damaged flat VMDK.

Read-only. Never modifies the file.

Premise
-------
The Babuk ESXi encryptor only damages the first 0x20000000 bytes (512 MiB) of a
file, then appends a 32-byte key. On a guest disk that means:

  * the PRIMARY GPT / MBR at LBA 0 is destroyed,
  * but the BACKUP GPT in the last sectors of the disk is untouched,
  * the NTFS boot sector at the start of a partition may be destroyed,
  * but NTFS keeps a BACKUP boot sector in the LAST sector of that partition,
  * and ext2/3/4 keeps BACKUP SUPERBLOCKS spread across the volume.

So the entire layout is recoverable from structures that live past the damage
line. This tool harvests them and prints the exact offsets and commands needed
to mount or carve each surviving partition.

Usage:
    python babuk_mapdisk.py <disk-flat.vmdk> [--deep]

--deep also sector-scans the whole disk for stray volume boot records
(slow: reads the entire file). The default scan reads only ~0.05% of the disk.
"""

import argparse
import os
import struct
import sys
import uuid

SECTOR = 512
ENC_LIMIT = 0x20000000       # 512 MiB damage line
KEY_LEN = 32

GPT_TYPES = {
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "EFI System Partition",
    "e3c9e316-0b5c-4db8-817d-f92df00215ae": "Microsoft Reserved",
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7": "Basic data (NTFS/exFAT/FAT)",
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac": "Windows Recovery",
    "0fc63daf-8483-4772-8e79-3d69d8477de4": "Linux filesystem",
    "e6d6d379-f507-44c2-a23c-238f2a3df928": "Linux LVM",
    "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f": "Linux swap",
    "21686148-6449-6e6f-744e-656564454649": "BIOS boot",
    "a19d880f-05fc-4d3b-a006-743f0f84911e": "Linux RAID",
    "933ac7e1-2eb4-4f13-b844-0e14e2aef915": "Linux /home",
}


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PiB"


def mixed_endian_guid(raw: bytes) -> str:
    """GPT stores the first three GUID fields little-endian."""
    return str(uuid.UUID(bytes_le=raw))


# --------------------------------------------------------------------------
# original (pre-encryption) size
# --------------------------------------------------------------------------

def original_size(path: str):
    size = os.path.getsize(path)
    rem = size % SECTOR
    passes = {0: 0, KEY_LEN: 1, 2 * KEY_LEN: 2}.get(rem)
    if passes is None:
        return size, None, rem
    return size - passes * KEY_LEN, passes, rem


# --------------------------------------------------------------------------
# GPT
# --------------------------------------------------------------------------

def parse_gpt_header(hdr: bytes):
    if hdr[:8] != b"EFI PART":
        return None
    return {
        "current_lba": struct.unpack_from("<Q", hdr, 0x18)[0],
        "backup_lba": struct.unpack_from("<Q", hdr, 0x20)[0],
        "first_usable": struct.unpack_from("<Q", hdr, 0x28)[0],
        "last_usable": struct.unpack_from("<Q", hdr, 0x30)[0],
        "disk_guid": mixed_endian_guid(hdr[0x38:0x48]),
        "entries_lba": struct.unpack_from("<Q", hdr, 0x48)[0],
        "num_entries": struct.unpack_from("<I", hdr, 0x50)[0],
        "entry_size": struct.unpack_from("<I", hdr, 0x54)[0],
    }


def read_backup_gpt(fh, disk_size: int):
    """The backup GPT header sits in the final sector of the disk."""
    last_lba_off = (disk_size // SECTOR - 1) * SECTOR
    fh.seek(last_lba_off)
    hdr = parse_gpt_header(fh.read(SECTOR))
    if not hdr:
        return None, []

    if not (0 < hdr["num_entries"] <= 512) or hdr["entry_size"] < 128:
        return hdr, []

    fh.seek(hdr["entries_lba"] * SECTOR)
    blob = fh.read(hdr["num_entries"] * hdr["entry_size"])

    parts = []
    for i in range(hdr["num_entries"]):
        e = blob[i * hdr["entry_size"]: (i + 1) * hdr["entry_size"]]
        if len(e) < 128 or e[:16] == b"\x00" * 16:
            continue
        type_guid = mixed_endian_guid(e[:16])
        first_lba, last_lba = struct.unpack_from("<QQ", e, 0x20)
        name = e[0x38:0x80].decode("utf-16-le", "replace").split("\x00")[0]
        parts.append({
            "index": i + 1,
            "type_guid": type_guid,
            "type": GPT_TYPES.get(type_guid, "unknown"),
            "first_lba": first_lba,
            "last_lba": last_lba,
            "start": first_lba * SECTOR,
            "size": (last_lba - first_lba + 1) * SECTOR,
            "name": name,
        })
    return hdr, parts


# --------------------------------------------------------------------------
# NTFS
# --------------------------------------------------------------------------

def parse_ntfs_vbr(vbr: bytes, part_start: int):
    if vbr[3:11] != b"NTFS    ":
        return None
    bps = struct.unpack_from("<H", vbr, 0x0B)[0]
    spc_raw = vbr[0x0D]
    if bps == 0 or bps % 512:
        return None
    # sectors-per-cluster is a signed power-of-two shift when >= 0x80
    spc = spc_raw if spc_raw < 0x80 else 1 << (256 - spc_raw)
    total_sectors = struct.unpack_from("<Q", vbr, 0x28)[0]
    mft_cluster = struct.unpack_from("<Q", vbr, 0x30)[0]
    mftmirr_cluster = struct.unpack_from("<Q", vbr, 0x38)[0]
    cluster = bps * spc
    if cluster == 0:
        return None
    return {
        "bytes_per_sector": bps,
        "sectors_per_cluster": spc,
        "cluster_size": cluster,
        "total_sectors": total_sectors,
        "volume_size": total_sectors * bps,
        "mft_offset_in_part": mft_cluster * cluster,
        "mft_offset_abs": part_start + mft_cluster * cluster,
        "mftmirr_offset_abs": part_start + mftmirr_cluster * cluster,
    }


def read_ntfs(fh, part_start: int, part_size: int, disk_size: int):
    """Try the primary VBR; fall back to the backup VBR in the last sector."""
    for label, off in (("primary", part_start),
                       ("backup", part_start + part_size - SECTOR)):
        if off < 0 or off + SECTOR > disk_size:
            continue
        fh.seek(off)
        info = parse_ntfs_vbr(fh.read(SECTOR), part_start)
        if info:
            info["vbr_source"] = label
            info["vbr_offset"] = off
            return info
    return None


# --------------------------------------------------------------------------
# ext2/3/4
# --------------------------------------------------------------------------

def parse_ext_sb(sb: bytes):
    if len(sb) < 0x60 or struct.unpack_from("<H", sb, 0x38)[0] != 0xEF53:
        return None
    log_bs = struct.unpack_from("<I", sb, 0x18)[0]
    if log_bs > 6:
        return None
    block_size = 1024 << log_bs
    blocks = struct.unpack_from("<I", sb, 0x04)[0]
    blocks_hi = struct.unpack_from("<I", sb, 0x150)[0] if len(sb) >= 0x154 else 0
    return {
        "block_size": block_size,
        "blocks": blocks | (blocks_hi << 32),
        "blocks_per_group": struct.unpack_from("<I", sb, 0x20)[0],
        "volume_size": (blocks | (blocks_hi << 32)) * block_size,
    }


def read_ext(fh, part_start: int, disk_size: int):
    off = part_start + 1024
    if off + 1024 > disk_size:
        return None
    fh.seek(off)
    info = parse_ext_sb(fh.read(1024))
    if not info:
        return None
    info["sb_offset"] = off
    # sparse_super keeps backups at group numbers 1, 3, 5, 7, 9, 25, 27, 49...
    gsize = info["blocks_per_group"] * info["block_size"]
    info["backup_superblocks"] = [
        {"group": g, "offset": part_start + g * gsize,
         "beyond_damage": part_start + g * gsize >= ENC_LIMIT}
        for g in (1, 3, 5, 7, 9, 25, 27, 49)
        if gsize and part_start + g * gsize < disk_size
    ]
    return info


# --------------------------------------------------------------------------
# fallback signature scan
# --------------------------------------------------------------------------

def scan_for_vbrs(fh, disk_size: int, step: int):
    """Sample `step`-aligned offsets looking for volume boot records."""
    hits = []
    off = 0
    while off + SECTOR <= disk_size:
        fh.seek(off)
        buf = fh.read(SECTOR)
        if len(buf) < SECTOR:
            break
        if buf[3:11] == b"NTFS    ":
            hits.append((off, "NTFS VBR"))
        elif buf[3:11] in (b"EXFAT   ",):
            hits.append((off, "exFAT VBR"))
        elif buf[:8] == b"EFI PART":
            hits.append((off, "GPT header"))
        off += step
    return hits


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Map a Babuk-damaged flat VMDK (read-only)")
    ap.add_argument("disk")
    ap.add_argument("--deep", action="store_true",
                    help="sector-by-sector scan of the whole disk (slow)")
    args = ap.parse_args()

    path = args.disk
    orig, passes, rem = original_size(path)
    on_disk = os.path.getsize(path)

    print("=" * 78)
    print(f"  {path}")
    print("=" * 78)
    print(f"  size on disk      : {on_disk:,} bytes ({human(on_disk)})")
    print(f"  size % 512        : {rem}")
    print(f"  encryption passes : {passes if passes is not None else 'UNKNOWN'}")
    print(f"  original size     : {orig:,} bytes ({human(orig)})")
    if orig > ENC_LIMIT:
        pct = 100.0 * (orig - ENC_LIMIT) / orig
        print(f"  damaged region    : 0x0 - 0x{ENC_LIMIT:x} ({human(ENC_LIMIT)})")
        print(f"  INTACT region     : 0x{ENC_LIMIT:x} - end  "
              f"({human(orig - ENC_LIMIT)}, {pct:.3f}% of the disk)")
    else:
        print("  *** file is <= 512 MiB: entirely encrypted, no keyless recovery ***")
        return 1

    if passes:
        with open(path, "rb") as fh:
            fh.seek(on_disk - KEY_LEN)
            print(f"  outer ephemeral key: {fh.read(KEY_LEN).hex()}")
            if passes == 2:
                fh.seek(on_disk - 2 * KEY_LEN)
                print(f"  inner ephemeral key: {fh.read(KEY_LEN).hex()}")

    with open(path, "rb") as fh:
        # ---- backup GPT ---------------------------------------------------
        print("\n" + "-" * 78)
        print("  BACKUP GPT (end of disk - past the damage line)")
        print("-" * 78)
        hdr, parts = read_backup_gpt(fh, orig)
        if hdr:
            print(f"  recovered. disk GUID {hdr['disk_guid']}")
            print(f"  usable LBA {hdr['first_usable']} - {hdr['last_usable']}, "
                  f"{len(parts)} partition(s)\n")
        else:
            print("  no backup GPT found (disk may be MBR, or sector size != 512)")
            parts = []

        # ---- per partition ------------------------------------------------
        for p in parts:
            print(f"  [{p['index']}] {p['type']}")
            if p["name"]:
                print(f"       name        : {p['name']}")
            print(f"       start offset: 0x{p['start']:x} ({p['start']:,})")
            print(f"       size        : {human(p['size'])}")
            print(f"       start LBA   : {p['first_lba']}   sectors: "
                  f"{p['last_lba'] - p['first_lba'] + 1}")
            damaged = p["start"] < ENC_LIMIT
            print(f"       head damaged: {'YES' if damaged else 'no - fully intact'}")

            ntfs = read_ntfs(fh, p["start"], p["size"], orig)
            if ntfs:
                print(f"       NTFS via {ntfs['vbr_source']} boot sector "
                      f"@0x{ntfs['vbr_offset']:x}")
                print(f"         cluster size : {ntfs['cluster_size']}")
                print(f"         volume size  : {human(ntfs['volume_size'])}")
                mft = ntfs["mft_offset_abs"]
                safe = mft >= ENC_LIMIT
                print(f"         $MFT at      : 0x{mft:x} ({human(mft)} into the disk)")
                print(f"         $MFT status  : "
                      f"{'INTACT - full file table survives' if safe else 'inside damaged region'}")
                if not safe:
                    mirr = ntfs["mftmirr_offset_abs"]
                    print(f"         $MFTMirr at  : 0x{mirr:x} "
                          f"({'intact' if mirr >= ENC_LIMIT else 'also damaged'})")

            ext = read_ext(fh, p["start"], orig)
            if ext:
                print(f"       ext2/3/4 superblock @0x{ext['sb_offset']:x}")
                print(f"         block size   : {ext['block_size']}")
                print(f"         volume size  : {human(ext['volume_size'])}")
                usable = [b for b in ext["backup_superblocks"] if b["beyond_damage"]]
                if usable:
                    b = usable[0]
                    print(f"         backup sb    : group {b['group']} @0x{b['offset']:x} (intact)")
                    print(f"         repair with  : e2fsck -b {b['group'] * ext['blocks_per_group']} "
                          f"-B {ext['block_size']} <device>")
            print()

        # ---- fallback scan -------------------------------------------------
        if not parts:
            step = SECTOR if args.deep else (1 << 20)
            print("-" * 78)
            print(f"  SIGNATURE SCAN (step {human(step)})")
            print("-" * 78)
            for off, what in scan_for_vbrs(fh, orig, step)[:40]:
                flag = "intact" if off >= ENC_LIMIT else "in damaged region"
                print(f"  0x{off:012x}  {what:<12} ({flag})")

    # ---- next steps --------------------------------------------------------
    print("=" * 78)
    print("  NEXT STEPS")
    print("=" * 78)
    print(f"""
  1. Expose the disk WITHOUT the appended key bytes, read-only (Linux):

       losetup -r --sizelimit {orig} -f --show {path}

     --sizelimit restores 512-byte alignment that the appended {(passes or 0) * KEY_LEN}
     bytes broke; -r guarantees the original is never written to.

  2. Expose each partition found above:

       losetup -r --offset <start offset> --sizelimit <size> -f --show {path}

  3. Mount read-only, or hand the offsets to a recovery tool:

       mount -o ro,loop,offset=<start>,sizelimit=<size> {path} /mnt/rec
       fls  -o <start LBA> {path}          # Sleuth Kit file listing
       # or point DMDE / R-Studio / UFS Explorer at the loop device

  4. Anything whose metadata lives inside the first 512 MiB is gone; carve it:

       photorec {path}
       bulk_extractor -o out {path}
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
