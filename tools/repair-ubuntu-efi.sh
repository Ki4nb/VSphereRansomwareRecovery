#!/bin/bash
# Repair the Ubuntu 25.10 "ubuntu-01" disk in place.
#
# Damage is limited to the first 512 MiB, which held the GPT and the EFI System
# Partition. /boot (LBA 2203648) and the LVM root (LBA 6397952) both start past
# the damage line and are CLEAN - no fsck, no data copying needed.
#
# Partition values below were read directly from the backup GPT located at the
# original 150 GiB device end (the VMDK was later expanded to 200 GiB, which is
# why the backup is not at the end of the device and gdisk cannot find it).
#
# REQUIRES the disk to be in "Dependent" mode, otherwise every write is
# discarded at power off.
#
# Usage: bash repair-ubuntu-efi.sh /dev/sda [--commit]
#        without --commit it only reports what it would do.

set -u
DEV="${1:?usage: repair-ubuntu-efi.sh /dev/sdX [--commit]}"
COMMIT="${2:-}"

P1_START=2048;    P1_END=2203647       # EFI System Partition
P2_START=2203648; P2_END=6397951       # /boot   ext4  UUID 93f9da1a-...
P3_START=6397952; P3_END=314572766     # LVM PV  -> ubuntu-vg/ubuntu-lv
ESP_ID=7B046308                        # fstab wants /boot/efi UUID 7B04-6308

say() { echo "  $*"; }
run() {
    if [ "$COMMIT" = "--commit" ]; then "$@"; else echo "  WOULD RUN: $*"; fi
}

echo "=== target $DEV ==="
[ -b "$DEV" ] || { echo "not a block device"; exit 1; }
say "size: $(blockdev --getsize64 "$DEV") bytes"
[ "$COMMIT" = "--commit" ] || echo "  *** DRY RUN - pass --commit to apply ***"

# --- 0. safety: confirm the LVM PV really is at P3_START before writing ------
echo
echo "=== verifying LVM PV label at partition 3 before touching anything ==="
LBL=$(dd if="$DEV" bs=512 skip=$((P3_START + 1)) count=1 2>/dev/null | head -c 8)
if [ "$LBL" != "LABELONE" ]; then
    echo "  ABORT: expected LABELONE at LBA $((P3_START+1)), found '$LBL'"
    echo "  The offsets are wrong - do not continue."
    exit 1
fi
say "LABELONE confirmed at LBA $((P3_START+1)) - offsets are correct"

# --- 1. rewrite the partition table ----------------------------------------
echo
echo "=== writing GPT (partition CONTENTS are not touched) ==="
run sgdisk --clear \
    --new=1:$P1_START:$P1_END   --typecode=1:EF00 --change-name=1:"" \
    --new=2:$P2_START:$P2_END   --typecode=2:8300 --change-name=2:"" \
    --new=3:$P3_START:$P3_END   --typecode=3:8300 --change-name=3:"" \
    "$DEV"
run partprobe "$DEV"
run sleep 2

# --- 2. recreate the destroyed ESP with its ORIGINAL volume id --------------
# Using -i $ESP_ID reproduces UUID 7B04-6308, so /etc/fstab needs no edit.
echo
echo "=== recreating the EFI System Partition (UUID preserved) ==="
run mkfs.vfat -F32 -n EFI -i "$ESP_ID" "${DEV}1"

# --- 3. mount the (clean) filesystems --------------------------------------
echo
echo "=== activating LVM and mounting ==="
run vgchange -ay
run mkdir -p /mnt/sys
run mount /dev/mapper/ubuntu--vg-ubuntu--lv /mnt/sys      # rw: replays journal
run mount "${DEV}2" /mnt/sys/boot
run mkdir -p /mnt/sys/boot/efi
run mount "${DEV}1" /mnt/sys/boot/efi

# --- 4. reinstall the bootloader -------------------------------------------
echo
echo "=== installing GRUB (EFI) ==="
if [ "$COMMIT" = "--commit" ]; then
    if [ -d /mnt/sys/usr/lib/grub/x86_64-efi ]; then
        say "target has its own grub modules - installing from chroot"
        for d in dev dev/pts proc sys run; do mount --bind "/$d" "/mnt/sys/$d"; done
        chroot /mnt/sys grub-install --target=x86_64-efi \
            --efi-directory=/boot/efi --bootloader-id=ubuntu --recheck
        chroot /mnt/sys grub-install --target=x86_64-efi \
            --efi-directory=/boot/efi --removable --recheck
        chroot /mnt/sys update-grub
        chroot /mnt/sys update-initramfs -u
        for d in run sys proc dev/pts dev; do umount "/mnt/sys/$d" 2>/dev/null; done
    else
        say "target grub modules missing - installing from the rescue system"
        grub-install --target=x86_64-efi --efi-directory=/mnt/sys/boot/efi \
            --boot-directory=/mnt/sys/boot --removable --recheck
    fi
else
    echo "  WOULD RUN: grub-install --target=x86_64-efi --efi-directory=/boot/efi (chroot)"
    echo "  WOULD RUN: grub-install ... --removable"
    echo "  WOULD RUN: update-grub && update-initramfs -u"
fi

# --- 5. netplan MAC pin ------------------------------------------------------
echo
echo "=== netplan MAC pinning check ==="
NP=/mnt/sys/etc/netplan/00-installer-config.yaml
if [ "$COMMIT" = "--commit" ] && [ -f "$NP" ]; then
    if grep -q "macaddress:" "$NP"; then
        say "MAC pin present: $(grep macaddress: "$NP" | tr -d ' ')"
        say "Either set the new VM's MAC to match, or remove the match: block."
        say "(left unchanged - your call)"
    fi
fi

# --- 6. done ----------------------------------------------------------------
echo
if [ "$COMMIT" = "--commit" ]; then
    sync
    umount /mnt/sys/boot/efi /mnt/sys/boot 2>/dev/null
    umount /mnt/sys 2>/dev/null
    echo "=================================================================="
    echo " DONE. Power off, detach the disk, attach it to a new VM with"
    echo " Firmware = EFI, and boot. Set the VM MAC to 00:00:5E:00:53:01"
    echo " or strip the match: block from netplan first."
    echo "=================================================================="
else
    echo "Dry run complete. Re-run with --commit once the disk is in Dependent mode."
fi
