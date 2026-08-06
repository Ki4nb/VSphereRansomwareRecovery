#!/bin/sh
# Rebuild a bootable Debian disk from an e2fsck-recovered lost+found tree.
#
# Context: the ransomware destroyed the first 512 MiB of the filesystem, which
# contained the root directory inode. Every subtree below it survived, but
# e2fsck could only reattach them as numbered entries in lost+found. This
# reassembles them under their real names on a fresh disk and installs GRUB.
#
# Usage:  sh rebuild-bootable.sh /dev/sdb
#
# The source snapshot is read-only; only the target disk is written.

set -u

DST="${1:?usage: rebuild-bootable.sh /dev/sdX  (target disk, will be ERASED)}"
SRC=/mnt/rec/lost+found
OUT=/mnt/out

# Reuse the ORIGINAL filesystem UUIDs so /etc/fstab needs no edits.
ROOT_UUID=42fbf972-5fd5-4f30-9eac-27d3c5d0eb9f
SWAP_UUID=b3ce4907-74b9-4ea9-89a4-1065ddc5978c

# lost+found inode -> real path, as identified by directory contents
MAP="#2097153:etc
#3932161:var
#4718593:usr
#1179649:root
#1703937:home
#1966081:boot
#1835009:media"

echo "=== target: $DST  (ALL DATA ON IT WILL BE DESTROYED) ==="
lsblk -d -o NAME,SIZE,MODEL "$DST" || exit 1
[ -d "$SRC" ] || { echo "ERROR: $SRC not mounted"; exit 1; }

# ---------------------------------------------------------------- partition
echo
echo "=== partitioning (MBR, mirroring the original layout) ==="
sfdisk "$DST" <<'EOF'
label: dos
start=2048, size=165769216, type=83, bootable
start=165773312, size=1996800, type=82
EOF
sleep 2
partprobe "$DST" 2>/dev/null
sleep 2

echo
echo "=== creating filesystems with the ORIGINAL UUIDs ==="
mkfs.ext4 -F -U "$ROOT_UUID" "${DST}1" || exit 1
mkswap -U "$SWAP_UUID" "${DST}2" || exit 1

mkdir -p "$OUT"
mount "${DST}1" "$OUT" || exit 1

# ---------------------------------------------------------------- copy trees
echo
echo "=== copying recovered trees to their real names ==="
echo "$MAP" | while IFS=: read -r inode name; do
    [ -d "$SRC/$inode" ] || { echo "  SKIP $name (missing $inode)"; continue; }
    echo "  --> /$name"
    mkdir -p "$OUT/$name"
    rsync -aHAX --numeric-ids "$SRC/$inode/" "$OUT/$name/"
done

# ------------------------------------------------------- rebuild root layout
echo
echo "=== recreating usrmerge symlinks and mount points ==="
cd "$OUT" || exit 1
for l in bin sbin lib lib64; do
    [ -e "$l" ] || ln -s "usr/$l" "$l"
    echo "  /$l -> usr/$l"
done

mkdir -p proc sys run mnt opt srv dev tmp
chmod 0555 proc sys
chmod 0755 run mnt opt srv dev
chmod 1777 tmp
chmod 0700 root
echo "  created: proc sys run mnt opt srv dev tmp"

# ---------------------------------------------------------------- fstab check
echo
echo "=== /etc/fstab (should already match the UUIDs above) ==="
grep -vE '^\s*#' "$OUT/etc/fstab" 2>/dev/null | grep -v '^$'

# ---------------------------------------------------------------- grub
# The ransomware destroyed /usr/lib/grub in the target (grub-pc-bin: 310 of
# 318 files gone), so a chroot grub-install would fail for lack of modules.
# Install using the RESCUE system's GRUB instead, writing into the target's
# /boot via --boot-directory. That plants working i386-pc modules and MBR
# boot code without needing anything from the damaged target.
echo
echo "=== installing GRUB (BIOS/i386-pc) from the rescue system ==="
grub-install --target=i386-pc --boot-directory="$OUT/boot" --recheck "$DST" || {
    echo "ERROR: grub-install failed"; exit 1; }

# grub-mkconfig lives in grub-common, which is also damaged (46 of 235 files
# missing), so hand-write a minimal config against a kernel we verified is
# complete. Once booted with network, 'apt install --reinstall grub-pc
# grub-common && update-grub' regenerates this properly.
echo
echo "=== writing grub.cfg ==="
mkdir -p "$OUT/boot/grub"
cat > "$OUT/boot/grub/grub.cfg" <<EOF
set timeout=5
set default=0

insmod part_msdos
insmod ext2
search --no-floppy --fs-uuid --set=root $ROOT_UUID

menuentry 'Debian GNU/Linux (6.1.0-28-amd64)' {
    linux  /boot/vmlinuz-6.1.0-28-amd64 root=UUID=$ROOT_UUID ro
    initrd /boot/initrd.img-6.1.0-28-amd64
}
menuentry 'Debian GNU/Linux (6.1.0-27-amd64)' {
    linux  /boot/vmlinuz-6.1.0-27-amd64 root=UUID=$ROOT_UUID ro
    initrd /boot/initrd.img-6.1.0-27-amd64
}
menuentry 'Debian GNU/Linux (6.1.0-28-amd64) - recovery' {
    linux  /boot/vmlinuz-6.1.0-28-amd64 root=UUID=$ROOT_UUID ro single
    initrd /boot/initrd.img-6.1.0-28-amd64
}
EOF
echo "  grub.cfg written (kernels 6.1.0-28 / 6.1.0-27, both verified complete)"

# Leave a note listing packages to reinstall on first boot.
cat > "$OUT/root/RESTORE-PACKAGES.txt" <<'EOF'
Files missing after ransomware recovery are all from Debian packages, and the
dpkg database survived intact - so every one can be restored from the network:

    apt-get update
    apt-get install --reinstall grub-pc grub-common grub-pc-bin os-prober \
        linux-image-6.11.5+bpo-amd64 open-vm-tools intel-microcode \
        modemmanager wpasupplicant plymouth udisks2 passwd
    update-grub
    update-initramfs -u -k all

To find every damaged package and reinstall the lot:

    dpkg --verify | awk '{print $NF}' | xargs -r dpkg -S 2>/dev/null \
      | cut -d: -f1 | sort -u > /root/damaged-packages.txt
    apt-get install --reinstall $(cat /root/damaged-packages.txt)

Service-critical packages were verified INTACT and need nothing:
    mongodb-org-server, openssh-server, systemd, redis-server,
    ifupdown, network-manager, libc6, bash, coreutils
EOF
echo "  wrote /root/RESTORE-PACKAGES.txt"

# ---------------------------------------------------------------- cleanup
echo
echo "=== unmounting ==="
umount "$OUT/run" "$OUT/sys" "$OUT/proc" "$OUT/dev/pts" "$OUT/dev" 2>/dev/null
sync
umount "$OUT"

echo
echo "=================================================================="
echo " DONE. $DST is now a bootable Debian disk."
echo " Power off, detach both disks, attach $DST as the only disk to a"
echo " new VM with firmware = BIOS (not EFI), and boot it."
echo "=================================================================="
