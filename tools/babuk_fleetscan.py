#!/usr/bin/env python3
"""
Fleet-wide survivability scan for Babuk-damaged flat VMDKs. Read-only.

For every *.babyk larger than the 512 MiB damage line, recover the partition
table from the backup GPT and report which partitions survived intact.

Usage:  python3 babuk_fleetscan.py <root> [<root> ...] [--csv out.csv]
"""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ir_mapdisk as M           # noqa: E402  (shared parsing logic)

ENC_LIMIT = M.ENC_LIMIT


def scan_disk(path):
    size = os.path.getsize(path)
    orig, passes, rem = M.original_size(path)
    if orig <= ENC_LIMIT:
        return None

    rows = []
    with open(path, "rb") as fh:
        hdr, parts = M.read_backup_gpt(fh, orig)
        for p in parts:
            intact = p["start"] >= ENC_LIMIT
            fs, detail = "", ""
            ext = M.read_ext(fh, p["start"], orig)
            if ext:
                fs = "ext2/3/4"
                detail = f"block={ext['block_size']}"
            else:
                ntfs = M.read_ntfs(fh, p["start"], p["size"], orig)
                if ntfs:
                    fs = f"NTFS({ntfs['vbr_source']} VBR)"
                    detail = f"$MFT {'intact' if ntfs['mft_offset_abs'] >= ENC_LIMIT else 'DAMAGED'}"
            rows.append({
                "disk": path,
                "disk_gib": round(orig / (1 << 30), 2),
                "passes": passes,
                "part": p["index"],
                "type": p["type"],
                "start": p["start"],
                "size_gib": round(p["size"] / (1 << 30), 2),
                "intact": "YES" if intact else "no (head damaged)",
                "fs": fs,
                "detail": detail,
                "sectors": orig // 512,
            })
        if not parts:
            rows.append({
                "disk": path, "disk_gib": round(orig / (1 << 30), 2),
                "passes": passes, "part": "-", "type": "NO BACKUP GPT",
                "start": 0, "size_gib": 0, "intact": "?", "fs": "", "detail": "",
                "sectors": orig // 512,
            })
    return rows


def main():
    roots, csv_out = [], None
    args = sys.argv[1:]
    while args:
        a = args.pop(0)
        if a == "--csv":
            csv_out = args.pop(0)
        else:
            roots.append(a)

    all_rows = []
    for root in roots:
        for dirpath, _, names in os.walk(root):
            for name in names:
                if not name.endswith(".babyk"):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    if os.path.getsize(path) <= ENC_LIMIT:
                        continue
                    rows = scan_disk(path)
                    if rows:
                        all_rows.extend(rows)
                except OSError as exc:
                    print(f"  !! {path}: {exc}", file=sys.stderr)

    cur = None
    total_intact = 0
    for r in all_rows:
        if r["disk"] != cur:
            cur = r["disk"]
            print(f"\n{os.path.basename(r['disk'])}")
            print(f"   {r['disk_gib']} GiB | {r['passes']} pass(es) | "
                  f"usable sectors for descriptor: {r['sectors']}")
        if r["intact"] == "YES":
            total_intact += r["size_gib"]
        print(f"   [{r['part']}] {r['type'][:30]:<30} {r['size_gib']:>9.2f} GiB  "
              f"start=0x{r['start']:<10x} {r['intact']:<18} {r['fs']} {r['detail']}")

    print(f"\n{'=' * 70}")
    print(f"total FULLY INTACT partition capacity: {total_intact:.2f} GiB")
    print(f"{'=' * 70}")

    if csv_out:
        with open(csv_out, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(all_rows[0].keys()))
            w.writeheader()
            w.writerows(all_rows)
        print(f"csv: {csv_out}")


if __name__ == "__main__":
    main()
