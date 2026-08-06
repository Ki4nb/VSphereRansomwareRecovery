#!/usr/bin/env python3
"""
Babuk/Babyk ESXi variant - damage triage tool.

Read-only. Never modifies the files it inspects.

For each file it answers the three questions that decide recovery strategy:

  1. Was it encrypted at all, once, or twice?
       Original ESXi/VMFS files are sector-aligned (size % 512 == 0).
       Each encryption pass appends a 32-byte ephemeral Curve25519 public key.
         size % 512 == 0  -> untouched
         size % 512 == 32 -> encrypted once
         size % 512 == 64 -> encrypted twice
  2. How much plaintext survives?
       The encryptor only processes the first 0x20000000 bytes (512 MiB).
       Everything past that offset is untouched -> recoverable without any key.
  3. Where do the appended ephemeral public keys live?
       Needed later if a master private key is ever obtained.

Usage:
    python babuk_triage.py <path> [-o report.csv]

<path> may be a single file or a directory (walked recursively).
Point it at a COPY or a read-only mount of the datastore, never the only copy.
"""

import argparse
import csv
import math
import os
import sys
from collections import Counter

ENC_LIMIT = 0x20000000          # 512 MiB - encryptor stops here
KEY_LEN = 32                    # appended Curve25519 public key
SAMPLE = 64 * 1024              # bytes per entropy probe

# Filesystem / container signatures worth reporting when found in the clear.
SIGNATURES = [
    (b"NTFS    ", "NTFS volume boot record", 3),
    (b"\x53\xEF", "ext2/3/4 superblock magic", 56),
    (b"EFI PART", "GPT header", 0),
    (b"XFSB", "XFS superblock", 0),
    (b"LVM2 001", "LVM2 physical volume label", 0),
    (b"\x64\x40\x00\x00", "VMFS-ish marker", 0),
]


def shannon(data: bytes) -> float:
    """Entropy in bits/byte. ~8.0 = ciphertext/compressed, <7.0 = likely structure."""
    if not data:
        return 0.0
    counts = Counter(data)
    n = len(data)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def probe(fh, offset: int, size: int, file_size: int) -> bytes:
    """Read up to `size` bytes at `offset`, clamped to the file."""
    if offset >= file_size:
        return b""
    fh.seek(offset)
    return fh.read(min(size, file_size - offset))


def classify_padding(size: int):
    """Map size % 512 to an encryption-pass count."""
    rem = size % 512
    if rem == 0:
        return 0, "untouched (sector-aligned, no appended key)"
    if rem == KEY_LEN:
        return 1, "encrypted ONCE (one 32-byte key appended)"
    if rem == 2 * KEY_LEN:
        return 2, "encrypted TWICE (two 32-byte keys appended)"
    return None, f"unknown (size %% 512 == {rem}; may not have been 512-aligned originally)"


def find_signatures(data: bytes, base: int):
    """Report known filesystem signatures inside a sampled buffer."""
    hits = []
    for magic, name, back in SIGNATURES:
        pos = data.find(magic)
        if pos != -1:
            hits.append(f"{name}@0x{base + pos - back:x}")
    return hits


def analyse(path: str) -> dict:
    size = os.path.getsize(path)
    passes, padding_note = classify_padding(size)

    # Size of the file as it was before any key blobs were appended.
    appended = (passes or 0) * KEY_LEN
    original_size = size - appended

    row = {
        "path": path,
        "size": size,
        "size_mod_512": size % 512,
        "encryption_passes": "?" if passes is None else passes,
        "padding_note": padding_note,
        "original_size": original_size,
        "encrypted_bytes": min(original_size, ENC_LIMIT),
        "plaintext_bytes": max(0, original_size - ENC_LIMIT),
    }

    row["plaintext_pct"] = (
        round(100.0 * row["plaintext_bytes"] / original_size, 4) if original_size else 0.0
    )

    with open(path, "rb") as fh:
        # --- entropy either side of the 512 MiB boundary -------------------
        head = probe(fh, 0, SAMPLE, size)
        row["entropy_head"] = round(shannon(head), 3)

        before = probe(fh, max(0, ENC_LIMIT - SAMPLE), SAMPLE, size)
        row["entropy_before_boundary"] = round(shannon(before), 3) if before else ""

        after = probe(fh, ENC_LIMIT, SAMPLE, size)
        row["entropy_after_boundary"] = round(shannon(after), 3) if after else ""

        # A clear entropy drop at exactly 512 MiB confirms the 0x20000000 limit.
        if before and after:
            row["boundary_confirmed"] = (
                "YES" if (row["entropy_before_boundary"] > 7.5
                          and row["entropy_after_boundary"] < 7.0) else "inconclusive"
            )
        else:
            row["boundary_confirmed"] = "n/a (file <= 512 MiB)"

        # --- recognisable structure in the surviving region ----------------
        sigs = []
        if original_size > ENC_LIMIT:
            for off in (ENC_LIMIT, ENC_LIMIT + (1 << 20), original_size // 2):
                if off < original_size:
                    sigs += find_signatures(probe(fh, off, SAMPLE, size), off)
        row["signatures_in_plaintext"] = "; ".join(dict.fromkeys(sigs))

        # --- appended ephemeral public keys --------------------------------
        # Outer key (applied LAST, must be undone FIRST) is the final 32 bytes.
        keys = []
        if passes:
            for i in range(passes):
                start = size - (i + 1) * KEY_LEN
                keys.append(probe(fh, start, KEY_LEN, size).hex())
        row["key_outer"] = keys[0] if len(keys) > 0 else ""
        row["key_inner"] = keys[1] if len(keys) > 1 else ""

        # For a >512 MiB file the inner key sits beyond the encrypted region,
        # so it is stored in the clear and is directly usable.
        row["inner_key_is_plaintext"] = (
            "yes" if (passes == 2 and original_size > ENC_LIMIT) else
            "no (re-encrypted by 2nd pass)" if passes == 2 else ""
        )

    # --- verdict -----------------------------------------------------------
    # The padding test alone is circumstantial: a file that was never
    # sector-aligned to begin with can land on any remainder. Cross-check it
    # against head entropy before declaring anything destroyed.
    head_looks_encrypted = row["entropy_head"] > 7.5

    if passes is None:
        row["verdict"] = (
            "UNKNOWN - size not 512-aligned and no key-sized padding; "
            + ("head is high-entropy, inspect manually"
               if head_looks_encrypted else
               "head is low-entropy, most likely NEVER ENCRYPTED")
        )
    elif passes == 0:
        row["verdict"] = "INTACT - no recovery needed"
    elif not head_looks_encrypted:
        row["verdict"] = (
            "SUSPECT - padding suggests encryption but head is low-entropy; "
            "verify by hand before treating as lost"
        )
    elif original_size <= ENC_LIMIT:
        row["verdict"] = "TOTAL LOSS without key - file is 100% encrypted"
    else:
        row["verdict"] = (
            f"PARTIAL RECOVERY - {row['plaintext_pct']}% plaintext survives; "
            "carve/rebuild filesystem from offset 0x20000000"
        )
    return row


FIELDS = [
    "path", "size", "size_mod_512", "encryption_passes", "padding_note",
    "original_size", "encrypted_bytes", "plaintext_bytes", "plaintext_pct",
    "entropy_head", "entropy_before_boundary", "entropy_after_boundary",
    "boundary_confirmed", "signatures_in_plaintext",
    "key_outer", "key_inner", "inner_key_is_plaintext", "verdict",
]


def iter_files(root: str):
    if os.path.isfile(root):
        yield root
        return
    for dirpath, _, names in os.walk(root):
        for name in names:
            yield os.path.join(dirpath, name)


def main() -> int:
    ap = argparse.ArgumentParser(description="Babuk ESXi damage triage (read-only)")
    ap.add_argument("path", help="file or directory to inspect")
    ap.add_argument("-o", "--out", default="babuk_triage.csv", help="CSV report path")
    ap.add_argument("--min-size", type=int, default=0, help="skip files smaller than N bytes")
    args = ap.parse_args()

    rows = []
    for path in iter_files(args.path):
        try:
            if os.path.getsize(path) < args.min_size:
                continue
            rows.append(analyse(path))
        except (OSError, PermissionError) as exc:
            print(f"  !! skipped {path}: {exc}", file=sys.stderr)

    rows.sort(key=lambda r: r["plaintext_bytes"], reverse=True)

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    recoverable = sum(r["plaintext_bytes"] for r in rows)
    lost = sum(1 for r in rows if r["verdict"].startswith("TOTAL"))
    intact = sum(1 for r in rows if r["verdict"].startswith("INTACT"))
    twice = sum(1 for r in rows if r["encryption_passes"] == 2)

    print(f"\nfiles inspected      : {len(rows)}")
    print(f"intact               : {intact}")
    print(f"encrypted twice      : {twice}")
    print(f"total loss w/o key   : {lost}")
    print(f"plaintext recoverable: {recoverable / (1 << 30):.2f} GiB")
    print(f"report               : {args.out}\n")

    for r in rows[:15]:
        print(f"{r['plaintext_bytes'] / (1 << 30):10.2f} GiB  {r['verdict'][:60]:<60} {r['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
