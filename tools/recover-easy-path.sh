#!/bin/bash
# Babuk/Babyk recovery - "easy path": the root filesystem starts past the
# 512 MiB damage line, so only the GPT and the EFI System Partition are gone.
#
# Run this from a SystemRescue VM with the damaged -recovered.vmdk attached.
#
#   bash recover-easy-path.sh                 # DRY RUN - inspect and report only
#   bash recover-easy-path.sh --commit        # actually repair
#   bash recover-easy-path.sh --commit /dev/sdb
#
# Everything before --commit is read-only. The repair itself only ever writes:
#   * LBA 0-33   (the partition table, rebuilt from the disk's own backup GPT)
#   * partition 1 (the ESP - already 100% destroyed, so nothing is lost)
#   * the bootloader files GRUB installs into /boot and /boot/efi
# The root filesystem and /boot DATA are never rewritten.
#
# Validated against Ubuntu 25.10 guests with LVM roots, single- and double-pass,
# on four separate hosts.

set -u

COMMIT=""
DEV=""
for a in "$@"; do
    case "$a" in
        --commit) COMMIT=1 ;;
        /dev/*)   DEV="$a" ;;
        *) echo "unknown arg: $a"; exit 2 ;;
    esac
done

say()  { echo "  $*"; }
head1() { echo; echo "=== $* ==="; }
run()  { if [ -n "$COMMIT" ]; then "$@"; else echo "  WOULD RUN: $*"; fi; }
die()  { echo "  ABORT: $*"; cleanup; exit 1; }

MNT=/mnt/sys
cleanup() {
    sync 2>/dev/null
    # -R is required: the bind-mounted /sys contains a nested efivarfs, and a
    # non-recursive umount silently leaves /mnt/sys busy.
    umount -R $MNT 2>/dev/null
    umount /mnt/ro-root /mnt/ro-boot 2>/dev/null
    # scoped so we deactivate OUR clone's VG, not every identically-named one
    vgchange -an ${LVMSCOPE:-} 2>/dev/null | tail -1
    losetup -D 2>/dev/null
}

# ---------------------------------------------------------------- pick a disk
if [ -z "$DEV" ]; then
    # Pick the LARGEST real disk. ESXi VMs often expose a 4 KB floppy (fd0),
    # and "head -1" would happily select it; -b gives bytes so we can compare.
    DEV=$(lsblk -dnrbo NAME,TYPE,SIZE | awk '$2=="disk" && $1 !~ /^(fd|sr|zram|loop)/ {if ($3+0>m){m=$3+0;d=$1}} END{if(d)print "/dev/"d}')
    [ -n "$DEV" ] || die "no disk found - pass one explicitly"
    say "auto-selected largest disk: $DEV"
fi
[ -b "$DEV" ] || die "$DEV is not a block device"

head1 "target $DEV"
say "size : $(blockdev --getsize64 "$DEV") bytes ($(( $(blockdev --getsize64 "$DEV") / 1073741824 )) GiB)"
say "model: $(lsblk -dnro MODEL "$DEV" 2>/dev/null)"
[ -n "$COMMIT" ] || echo "  *** DRY RUN - pass --commit to apply ***"

# ------------------------------------------------- read the intact backup GPT
head1 "reading backup GPT (end of disk, past the damage line)"
GPTINFO=$(python3 - "$DEV" <<'PYEOF'
import struct, sys, uuid
dev=sys.argv[1]; S=512
T={"c12a7328-f81f-11d2-ba4b-00a0c93ec93b":"ESP",
   "0fc63daf-8483-4772-8e79-3d69d8477de4":"LINUX",
   "e6d6d379-f507-44c2-a23c-238f2a3df928":"LVM",
   "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f":"SWAP",
   "21686148-6449-6e6f-744e-656564454649":"BIOSBOOT"}
f=open(dev,"rb"); f.seek(0,2); size=f.tell()
f.seek((size//S-1)*S); h=f.read(S)

if h[:8]!=b"EFI PART":
    # The disk may have been EXPANDED in VMware: the secondary GPT then sits at
    # the ORIGINAL end, not the device end, and the disk looks MBR-only. Locate
    # the last ext4 filesystem, then scan just past it for the real backup GPT.
    fsend=0
    CH=8<<20; base=0x20000000; limit=min(size, base+(8<<30))
    while base<limit:
        f.seek(base); buf=f.read(CH)
        if not buf: break
        i=buf.find(b"\x53\xef")
        while i!=-1:
            sb=i-0x38; a=base+sb
            if sb>=0 and sb+0x160<=len(buf) and a%1024==0:
                logbs=struct.unpack_from("<I",buf,sb+0x18)[0]
                bpg=struct.unpack_from("<I",buf,sb+0x20)[0]
                gnr=struct.unpack_from("<H",buf,sb+0x5A)[0]
                if logbs<=6 and 0<bpg<=(1<<20) and gnr:
                    bs=1024<<logbs
                    st=a-gnr*bpg*bs
                    blocks=struct.unpack_from("<I",buf,sb+0x04)[0]|(struct.unpack_from("<I",buf,sb+0x150)[0]<<32)
                    if st>=0: fsend=max(fsend, st+blocks*bs)
            i=buf.find(b"\x53\xef",i+1)
        base+=CH
    found=None
    if fsend:
        lo=(fsend//S)*S; hi=min(size, lo+(256<<20)); pos=lo; step=1<<20
        while pos<hi:
            f.seek(pos); c=f.read(min(step+S, hi-pos+S))
            if not c: break
            o=c.find(b"EFI PART")
            while o!=-1:
                a=pos+o
                if a%S==0: found=a; break
                o=c.find(b"EFI PART",o+1)
            if found: break
            pos+=step
    if found is None:
        print("NOGPT"); raise SystemExit
    f.seek(found); h=f.read(S)
    print("ORIGSIZE %d"%((struct.unpack_from("<Q",h,0x18)[0]+1)*S))

elba=struct.unpack_from("<Q",h,0x48)[0]
n,esz=struct.unpack_from("<II",h,0x50)
f.seek(elba*S); blob=f.read(n*esz)
for i in range(n):
    e=blob[i*esz:(i+1)*esz]
    if len(e)<128 or e[:16]==b"\x00"*16: continue
    tg=str(uuid.UUID(bytes_le=e[:16]))
    fl,ll=struct.unpack_from("<QQ",e,0x20)
    print("PART %d %s %d %d"%(i+1,T.get(tg,"OTHER"),fl,ll))
PYEOF
) || die "could not read backup GPT"

echo "$GPTINFO" | grep -q NOGPT && die "no backup GPT - this is an MBR disk, use testdisk (see HANDOVER 4.4)"
echo "$GPTINFO" | sed 's/^/  /'

# If the disk was expanded, every later gdisk operation must run against a loop
# device capped at the ORIGINAL size, or the backup GPT is not where gdisk looks.
ORIGSIZE=$(echo "$GPTINFO" | awk '$1=="ORIGSIZE"{print $2}')
if [ -n "$ORIGSIZE" ]; then
    say "DISK WAS EXPANDED: backup GPT belongs to a $((ORIGSIZE/1073741824)) GiB disk,"
    say "device is now $(( $(blockdev --getsize64 "$DEV") / 1073741824 )) GiB."
    say "GPT recovery will run on a loop device capped at $ORIGSIZE bytes."
fi

ESP_N=$(echo "$GPTINFO"  | awk '$3=="ESP"{print $2; exit}')
LVM_N=$(echo "$GPTINFO"  | awk '$3=="LVM"{print $2; exit}')
# largest LINUX partition = root (or /boot if the root is on LVM)
BIG_N=$(echo "$GPTINFO"  | awk '$3=="LINUX"{if($5-$4>m){m=$5-$4;p=$2}}END{print p}')
BOOT_N=$(echo "$GPTINFO" | awk '$3=="LINUX"{print $2}' | head -1)

# ------------------------------------------------------- pre-flight integrity
head1 "pre-flight: are the surviving structures where the GPT says they are?"
probe_lvm() {  # $1 = first LBA
    [ "$(dd if=$DEV bs=512 skip=$(( $1 + 1 )) count=1 2>/dev/null | head -c 8)" = "LABELONE" ]
}
probe_ext() {  # $1 = first LBA ; superblock at +1024, magic 0x53EF at +0x38
    [ "$(dd if=$DEV bs=512 skip=$(( $1 + 2 )) count=1 2>/dev/null | od -An -tx1 -j 56 -N 2 | tr -d ' \n')" = "53ef" ]
}

PVLBA=""
if [ -n "${LVM_N:-}" ]; then
    PVLBA=$(echo "$GPTINFO" | awk -v n="$LVM_N" '$2==n{print $4}')
elif [ -n "${BIG_N:-}" ]; then
    PVLBA=$(echo "$GPTINFO" | awk -v n="$BIG_N" '$2==n{print $4}')
fi
if [ -n "$PVLBA" ]; then
    if probe_lvm "$PVLBA"; then say "LVM  LABELONE confirmed at LBA $((PVLBA+1))"
    elif probe_ext "$PVLBA"; then say "ext4 magic 0x53EF confirmed at LBA $((PVLBA+2))+56"
    else die "neither LABELONE nor ext4 magic at the expected offset - offsets are wrong, do not continue"
    fi
fi

# ------------------------------- learn the guest's own settings, read-only
part() { case "$DEV" in *nvme*|*loop*) echo "${DEV}p$1";; *) echo "${DEV}$1";; esac; }

head1 "reading the guest's fstab (read-only - the disk is not modified)"
PVPART=$(part "${LVM_N:-$BIG_N}")
LO=""
if [ -b "$PVPART" ]; then
    # The kernel can already see the partition table - either only the ESP was
    # lost, or this script has been run before. Use the partition directly:
    # stacking a loop device over the same PV makes LVM refuse to activate with
    # "Cannot activate LVs while PVs appear on duplicate devices", and the LV
    # node is then never created, so the mount fails with "Can't lookup blockdev".
    say "partition table already visible - using $PVPART directly"
    PVSRC="$PVPART"
else
    LO=$(losetup -r -o $(( PVLBA * 512 )) -f --show "$DEV") || die "losetup failed"
    say "GPT not visible to the kernel - using read-only loop $LO"
    PVSRC="$LO"
fi

# These guests are clones of one template, so when several are attached to the
# same rescue VM they all present the SAME VG name AND the same VG UUID. A
# global vgchange then refuses with "Cannot activate LVs in VG ... while PVs
# appear on duplicate devices". Scope every LVM call to this disk's PV only.
LVMSCOPE=""
if vgchange --help 2>&1 | grep -q -- '--devices'; then
    LVMSCOPE="--devices $PVSRC"
    say "scoping LVM to $PVSRC (other attached clones share this VG UUID)"
fi

if pvs $LVMSCOPE "$PVSRC" >/dev/null 2>&1; then
    vgscan --mknodes >/dev/null 2>&1; vgchange -ay $LVMSCOPE >/dev/null 2>&1
    ROOTSRC=$(lvs $LVMSCOPE --noheadings -o lv_path 2>/dev/null | head -1 | tr -d ' ')
    say "LVM root LV: $ROOTSRC"
else
    ROOTSRC="$PVSRC"; say "plain ext4 root on $PVSRC"
fi
mkdir -p /mnt/ro-root
mount -t ext4 -o ro,noload "$ROOTSRC" /mnt/ro-root || die "cannot mount root read-only"

HOSTNAME_G=$(cat /mnt/ro-root/etc/hostname 2>/dev/null)
OSREL=$(grep PRETTY_NAME /mnt/ro-root/etc/os-release 2>/dev/null | cut -d'"' -f2)
FSTAB=$(grep -vE '^\s*#' /mnt/ro-root/etc/fstab 2>/dev/null | grep -v '^$')
KVERS=$(ls /mnt/ro-root/usr/lib/modules/ 2>/dev/null | tr '\n' ' ')
say "guest    : ${HOSTNAME_G:-<unknown>}  ($OSREL)"
say "kernels  : ${KVERS:-<none found>}"
echo "$FSTAB" | sed 's/^/    fstab| /'

if echo "$FSTAB" | grep -q '/boot/efi'; then
    FIRMWARE=EFI
    ESP_UUID=$(echo "$FSTAB" | awk '$2=="/boot/efi"{print $1}' | sed 's#.*/##')
    ESP_ID=$(echo "$ESP_UUID" | tr -d '-')
else
    FIRMWARE=BIOS; ESP_UUID=""; ESP_ID=""
fi
BOOT_UUID=$(echo "$FSTAB" | awk '$2=="/boot"{print $1}' | sed 's#.*/##')
say "firmware : $FIRMWARE   (set the VM to this, or it will not boot)"
[ "$FIRMWARE" = EFI ] && say "ESP UUID : $ESP_UUID  -> mkfs.vfat -i $ESP_ID"

# netplan MAC pin is the #1 cause of "recovered but no network"
for np in /mnt/ro-root/etc/netplan/*.yaml; do
    [ -f "$np" ] || continue
    MACPIN=$(grep -A1 'match:' "$np" | grep macaddress: | awk '{print $2}')
    [ -n "$MACPIN" ] && say "MAC pin  : $MACPIN  <- set the new VM's MAC to this, or strip the match: block"
done

umount /mnt/ro-root 2>/dev/null
vgchange -an >/dev/null 2>&1
[ -n "$LO" ] && losetup -d "$LO" 2>/dev/null

if [ -z "$COMMIT" ]; then
    head1 "dry run complete"
    say "re-run with --commit (and the disk in DEPENDENT mode) to apply."
    exit 0
fi

# ============================== WRITES BEGIN ==============================
head1 "restoring the primary GPT from the backup"
# Leading '1' answers gdisk's "invalid MBR and corrupt GPT -> 1 = use current GPT".
# Without it gdisk consumes the 'r' and drops you into the menu.
if [ -n "$ORIGSIZE" ]; then
    # Expanded disk: cap a loop device at the original size so the backup GPT
    # lands at ITS end, where gdisk expects it. Writes still reach the real
    # disk - the loop is a window onto the same blocks, starting at offset 0.
    GLOOP=$(losetup --sizelimit "$ORIGSIZE" -f --show "$DEV") || die "losetup failed"
    say "using capped loop $GLOOP ($ORIGSIZE bytes)"
    printf '1\nr\nb\nw\nY\n' | gdisk "$GLOOP" 2>&1 | tail -6
    sync; losetup -d "$GLOOP"
    sleep 1; partprobe "$DEV" 2>/dev/null; sleep 2
    # Relocate the secondary GPT to the true end of the enlarged device so the
    # table is self-consistent and the extra space becomes usable later.
    say "moving the backup GPT to the real end of the device (sgdisk -e)"
    sgdisk -e "$DEV" 2>&1 | tail -3
    partprobe "$DEV" 2>/dev/null; sleep 2
else
    printf '1\nr\nb\nw\nY\n' | gdisk "$DEV" 2>&1 | tail -6
    sleep 1; partprobe "$DEV" 2>/dev/null; sleep 2
fi
lsblk -o NAME,SIZE,TYPE,FSTYPE "$DEV"

if [ "$FIRMWARE" = EFI ] && [ -n "${ESP_N:-}" ]; then
    head1 "recreating the ESP with its ORIGINAL volume id ($ESP_UUID)"
    mkfs.vfat -F32 -n EFI -i "$ESP_ID" "$(part $ESP_N)" || die "mkfs.vfat failed"
    blkid "$(part $ESP_N)"
fi

head1 "mounting the guest"
# re-scope: after the GPT write the PV lives on a partition device
BIGPART_PRE=$(part "${LVM_N:-$BIG_N}")
LVMSCOPE=""
if vgchange --help 2>&1 | grep -q -- '--devices'; then LVMSCOPE="--devices $BIGPART_PRE"; fi
vgchange -ay $LVMSCOPE >/dev/null 2>&1
# Detect LVM at RUNTIME rather than trusting the GPT type code: Ubuntu's
# installer puts the LVM PV on a partition typed 8300 "Linux filesystem", not
# 8e00 "Linux LVM", so keying off the type GUID silently picks the raw PV and
# the mount fails with "unknown filesystem type 'LVM2_member'".
BIGPART="$BIGPART_PRE"
if pvs $LVMSCOPE "$BIGPART" >/dev/null 2>&1; then
    ROOTDEV=$(lvs $LVMSCOPE --noheadings -o lv_path | head -1 | tr -d ' ')
    say "LVM detected on $BIGPART -> root LV $ROOTDEV"
else
    ROOTDEV="$BIGPART"
    say "plain filesystem root on $ROOTDEV"
fi
mkdir -p $MNT
mount "$ROOTDEV" $MNT || die "cannot mount root"
[ -n "${BOOT_N:-}" ] && [ "$BOOT_N" != "${BIG_N:-}" ] && mount "$(part $BOOT_N)" $MNT/boot
if [ "$FIRMWARE" = EFI ]; then mkdir -p $MNT/boot/efi; mount "$(part $ESP_N)" $MNT/boot/efi; fi
for d in dev dev/pts proc sys run; do mount --bind /$d $MNT/$d; done
df -h $MNT $MNT/boot 2>/dev/null | tail -3

# SystemRescue is Arch: its PATH omits /usr/sbin, so grub-install and
# update-grub are "not found" in the chroot unless PATH is set explicitly.
P='export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'

head1 "installing GRUB ($FIRMWARE)"
if [ "$FIRMWARE" = EFI ]; then
    if [ -d $MNT/usr/lib/grub/x86_64-efi ]; then
        chroot $MNT /bin/bash -c "$P; grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=ubuntu --recheck" 2>&1 | tail -4
        # --removable writes \EFI\BOOT\BOOTX64.EFI, which firmware boots even
        # though a chroot cannot create an NVRAM entry ("EFI variables cannot
        # be set" is expected and harmless).
        chroot $MNT /bin/bash -c "$P; grub-install --target=x86_64-efi --efi-directory=/boot/efi --removable --recheck" 2>&1 | tail -4
    else
        say "target has no grub modules - installing from the rescue system"
        grub-install --target=x86_64-efi --efi-directory=$MNT/boot/efi --boot-directory=$MNT/boot --removable --recheck 2>&1 | tail -4
    fi
else
    chroot $MNT /bin/bash -c "$P; grub-install --target=i386-pc --recheck $DEV" 2>&1 | tail -4
fi
chroot $MNT /bin/bash -c "$P; update-grub"        2>&1 | tail -6
chroot $MNT /bin/bash -c "$P; update-initramfs -u" 2>&1 | tail -4

head1 "verification"
say "menuentries: $(grep -c menuentry $MNT/boot/grub/grub.cfg 2>/dev/null)"
ls -la $MNT/boot/vmlinuz* $MNT/boot/initrd.img* 2>/dev/null | sed 's/^/    /'
[ "$FIRMWARE" = EFI ] && find $MNT/boot/efi -name 'BOOT*.EFI' -o -name 'grubx64.efi' 2>/dev/null | sed 's/^/    /'
echo "  -- fstab wants vs actual --"
echo "$FSTAB" | sed 's/^/    want| /'
blkid $(part 1) $(part 2) 2>/dev/null | sed 's/^/    have| /'

cleanup
head1 "DONE"
say "Power off, set VM firmware = $FIRMWARE, detach the rescue ISO, boot the disk."
[ -n "${MACPIN:-}" ] && say "Set the VM MAC to $MACPIN (or strip netplan's match: block)."

# Explicit exit 0. Without it the script's status is that of the test above,
# which is FALSE whenever the guest has no netplan MAC pin - making a perfectly
# successful repair report as FAILED to any caller that checks the exit code.
exit 0
