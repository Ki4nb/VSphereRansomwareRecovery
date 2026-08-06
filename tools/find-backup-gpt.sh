#!/bin/sh
# READ-ONLY: locate the backup GPT on a disk that was EXPANDED in VMware.
#
#   sh find-backup-gpt.sh /path/to/disk-flat.vmdk.babyk
#
# When a VMDK is grown, the secondary GPT stays at the ORIGINAL end of the disk,
# not the new device end - so `gdisk` finds nothing and the disk looks MBR-only.
# The fix is to cap a loop device at the original size so the backup lands where
# the kernel expects it (HANDOVER 4.4). This finds that original size.
#
# Strategy: derive an estimate from the last ext4 filesystem's own geometry
# (start + size), then sector-scan a window around it for "EFI PART".

F="${1:?usage: find-backup-gpt.sh <flat-file>}"
python3 - "$F" <<'PYEOF'
import sys, struct
p = sys.argv[1]
f = open(p, "rb")
f.seek(0, 2); dev = f.tell()
rem = dev % 512
orig_file = dev - rem
print("  file size        : %d B (%.2f GiB), %% 512 = %d -> %d pass(es)"
      % (dev, dev/(1<<30), rem, rem//32))
print("  usable extent    : %d B (%.2f GiB)" % (orig_file, orig_file/(1<<30)))

ENC = 0x20000000
# --- 1. find ext4 superblocks and back-calculate each filesystem's extent ----
fs = {}
CH = 8 << 20
base = ENC
limit = min(orig_file, ENC + (8 << 30))
while base < limit:
    f.seek(base); buf = f.read(CH)
    if not buf: break
    i = buf.find(b"\x53\xef")
    while i != -1:
        sb = i - 0x38; a = base + sb
        if sb >= 0 and sb + 0x60 <= len(buf) and a % 1024 == 0:
            logbs = struct.unpack_from("<I", buf, sb+0x18)[0]
            bpg   = struct.unpack_from("<I", buf, sb+0x20)[0]
            gnr   = struct.unpack_from("<H", buf, sb+0x5A)[0]
            if logbs <= 6 and 0 < bpg <= (1 << 20) and gnr:
                bs = 1024 << logbs
                start = a - gnr*bpg*bs
                blocks = struct.unpack_from("<I", buf, sb+0x04)[0]
                hi = struct.unpack_from("<I", buf, sb+0x150)[0] if sb+0x154 <= len(buf) else 0
                blocks |= hi << 32
                if start >= 0:
                    fs[start] = blocks * bs
        i = buf.find(b"\x53\xef", i+1)
    base += CH

if not fs:
    print("  no ext4 filesystems found - cannot estimate the original end")
    raise SystemExit(1)

print("  filesystems found:")
for s in sorted(fs):
    print("     start 0x%-12x size %10.2f GiB  end 0x%x" % (s, fs[s]/(1<<30), s+fs[s]))

last_end = max(s + fs[s] for s in fs)
print("  last filesystem ends at 0x%x (%.2f GiB)" % (last_end, last_end/(1<<30)))

# --- 2. sector-scan a window past that end for the backup GPT header --------
# GPT keeps 32 sectors of entries + 1 header sector at the very end of the disk,
# so the header sits a little past the final partition.
lo = (last_end // 512) * 512
hi = min(orig_file, lo + (256 << 20))
print("  scanning 0x%x - 0x%x for 'EFI PART' ..." % (lo, hi))
hit = None
step = 1 << 20
pos = lo
while pos < hi:
    f.seek(pos)
    chunk = f.read(min(step + 512, hi - pos + 512))
    if not chunk: break
    off = chunk.find(b"EFI PART")
    while off != -1:
        a = pos + off
        if a % 512 == 0:
            hit = a; break
        off = chunk.find(b"EFI PART", off+1)
    if hit: break
    pos += step

if hit is None:
    print("  NO backup GPT found in that window.")
    print("  Either the disk is genuinely MBR, or the window needs widening.")
    raise SystemExit(2)

f.seek(hit); hdr = f.read(512)
cur  = struct.unpack_from("<Q", hdr, 0x18)[0]
bak  = struct.unpack_from("<Q", hdr, 0x20)[0]
firstu, lastu = struct.unpack_from("<QQ", hdr, 0x28)[0:2]
print()
print("  FOUND backup GPT header at 0x%x (LBA %d)" % (hit, hit//512))
print("     header says its own LBA   : %d" % cur)
print("     header says primary LBA   : %d" % bak)
print("     usable LBA range          : %d - %d" % (firstu, lastu))
orig_size = (cur + 1) * 512
print()
print("  ORIGINAL DISK SIZE = %d bytes (%.2f GiB)" % (orig_size, orig_size/(1<<30)))
print("  current extent     = %d bytes (%.2f GiB)" % (orig_file, orig_file/(1<<30)))
print()
print("  Repair like this (read-only first):")
print("     losetup -r --sizelimit %d -f --show <dev>     # -> /dev/loopN" % orig_size)
print("     printf '1\\nr\\nb\\nw\\nY\\n' | gdisk /dev/loopN")
print("  then drop -r to commit, and afterwards run 'sgdisk -e <dev>' on the")
print("  full-size device to move the backup GPT to the real end.")
PYEOF
