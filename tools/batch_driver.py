#!/usr/bin/env python3
"""
Drive a batch repair: push the manifest and scripts into the rescue VM, start
the repair loop, and report progress.

Standard library only; the SSH plumbing is imported from esxi_run.py alongside.

    # dry run every disk attached to the rescue VM
    python3 batch_driver.py --esxi HOST --rescue RESCUE_IP --key ~/.ssh/ir_key

    # repair for real, skipping some guests
    python3 batch_driver.py --esxi HOST --rescue RESCUE_IP --key ~/.ssh/ir_key \
        --commit --exclude ubuntu-07 --exclude "app server 02"

    # check on a run already in flight
    python3 batch_driver.py --rescue RESCUE_IP --key ~/.ssh/ir_key --status

The manifest is written by make-batch-rescue-vm.sh onto the datastore, next to
the rescue VM's vmx. It maps each SCSI unit to a guest, which is the only
reliable way to tell these disks apart: the fleets are usually clones, so they
share filesystem UUIDs, LVM VG names and even LVM UUIDs, and most of them are
the same size.

Excluded guests are marked rather than removed, because batch-repair.sh matches
Linux SCSI hosts to vmx controllers by comparing each controller's complete set
of unit:size pairs. Deleting a line changes that signature and unmatches the
whole controller - which silently skipped six disks the first time it happened.
"""

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import esxi_run as R  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

STATUS_CMD = (
    'echo -n "batch running : "; ps aux 2>/dev/null | grep -c "[b]atch-repair.sh"; '
    'echo -n "disks complete: "; grep -l "=== DONE ===" /tmp/log.* 2>/dev/null | wc -l; '
    'echo "--- tail of /tmp/batch.out ---"; tail -15 /tmp/batch.out 2>/dev/null'
)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--esxi", help="ESXi host holding the manifest")
    ap.add_argument("--rescue", required=True, help="rescue VM address")
    ap.add_argument("--key")
    ap.add_argument("--password")
    ap.add_argument("--manifest", default="/vmfs/volumes/*/RESCUE-BATCH/manifest.txt")
    ap.add_argument("--commit", action="store_true", help="repair for real")
    ap.add_argument("--exclude", action="append", default=[],
                    help="guest to skip; repeatable")
    ap.add_argument("--status", action="store_true", help="report on a run in flight")
    args = ap.parse_args()

    conn = dict(key=args.key, password=args.password)

    if args.status:
        print(R.ssh(args.rescue, STATUS_CMD, **conn).stdout, end="")
        return 0

    if not args.esxi:
        ap.error("--esxi is required unless --status")

    # ---- fetch the manifest from the datastore -----------------------------
    man = R.ssh(args.esxi, f"cat {args.manifest}", **conn).stdout
    rows = [l for l in man.splitlines() if l.strip()]
    if not rows:
        sys.exit("manifest empty or unreadable - has the batch rescue VM been created?")
    print(f"manifest: {len(rows)} disks")

    # ---- mark exclusions ---------------------------------------------------
    for i, line in enumerate(rows):
        f = line.split(" ", 4)
        if len(f) >= 5 and f[4].strip() in args.exclude:
            print(f"  excluding: {f[4].strip()}")
            f[4] = "#SKIP#" + f[4]
            rows[i] = " ".join(f)

    # A trailing newline is mandatory: without it the driver's `while read`
    # drops the final record, which also corrupts that controller's signature.
    body = "\n".join(rows) + "\n"

    tmp = os.path.join(HERE, ".manifest.tmp")
    with open(tmp, "w", newline="\n") as fh:
        fh.write(body)
    try:
        R.upload(args.rescue, tmp, "/tmp/manifest.txt", **conn)
        print("  pushed /tmp/manifest.txt")
    finally:
        os.unlink(tmp)

    for src, dest in (("recover-easy-path.sh", "/tmp/rec.sh"),
                      ("batch-repair.sh", "/tmp/batch-repair.sh")):
        R.upload(args.rescue, os.path.join(HERE, src), dest, **conn)
        print(f"  pushed {dest}")

    chk = R.ssh(args.rescue, "bash -n /tmp/rec.sh && bash -n /tmp/batch-repair.sh && echo ok",
                **conn)
    if "ok" not in chk.stdout:
        sys.exit(f"remote syntax check failed: {chk.stderr.strip()}")

    flag = "--commit" if args.commit else ""
    print(f"\n=== launching batch ({'COMMIT' if args.commit else 'DRY RUN'}) ===")
    # Detached on purpose: a long job tied to the SSH channel dies with SIGHUP
    # if that channel drops, which killed a 32-disk run at disk 20.
    # The channel sometimes stays open even though the job is detached, so a
    # timeout here is expected and means nothing about whether it started.
    try:
        R.ssh(args.rescue,
              f"cd /tmp && setsid nohup bash /tmp/batch-repair.sh /tmp/manifest.txt {flag} "
              f"> /tmp/batch.out 2>&1 < /dev/null & echo started",
              timeout=30, **conn)
    except subprocess.TimeoutExpired:
        pass
    print("  running detached; poll with --status")
    return 0


if __name__ == "__main__":
    sys.exit(main())
