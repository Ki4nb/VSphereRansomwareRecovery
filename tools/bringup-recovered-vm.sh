#!/bin/sh
# Bring up a Babuk-renamed-but-NEVER-ENCRYPTED VM. Runs ON the ESXi host.
#
#   sh bringup-recovered-vm.sh <vm-folder> <portgroup>            # dry run
#   sh bringup-recovered-vm.sh <vm-folder> <portgroup> --commit   # do it
#
#   sh bringup-recovered-vm.sh ubuntu-01 "VM Network" --commit
#   sh bringup-recovered-vm.sh "app server 02" "VM Network" --commit --repaired
#
# These disks have size % 512 == 0: the encryptor renamed them but never wrote
# a byte, so the primary GPT/MBR is still intact and NO disk repair is needed.
# All this does is repoint the .vmx at the regenerated descriptor, replace the
# dead vCenter dvs binding with a standard portgroup, pin the original MAC, and
# register + power on. It never writes to the flat disk.
#
# Refuses to touch anything whose size % 512 != 0 - those need the rescue-VM
# easy path (tools/recover-easy-path.sh) instead.

NAME="${1:?usage: bringup-recovered-vm.sh <vm-folder> <portgroup> [--commit] [--repaired]}"
PG="${2:?missing portgroup, e.g. \"VM Network\"}"
COMMIT=""
REPAIRED=""
for a in "$@"; do
  case "$a" in
    --commit)   COMMIT="--commit" ;;
    --repaired) REPAIRED=1 ;;
  esac
done

say() { echo "  $*"; }
die() { echo "  ABORT: $*"; exit 1; }

# ---------------------------------------------------------------- locate it
DIR=""
for v in /vmfs/volumes/*; do
  [ -d "$v" ] && [ ! -L "$v" ] || continue
  [ -d "$v/$NAME" ] && DIR="$v/$NAME" && break
done
[ -n "$DIR" ] || die "VM folder '$NAME' not found under /vmfs/volumes"
echo "=== $NAME ==="
say "folder: $DIR"

FLAT=$(ls "$DIR"/*-flat.vmdk.babyk 2>/dev/null | head -1)
[ -n "$FLAT" ] || die "no *-flat.vmdk.babyk in $DIR"

# ------------------------------------------------- refuse if actually encrypted
S=$(ls -l "$FLAT" | awk '{print $5}')
R=$((S % 512))
if [ "$R" -eq 0 ]; then
    say "size % 512 == 0 -> never encrypted"
elif [ -n "$REPAIRED" ]; then
    # The trailing key bytes are never removed, so a repaired disk still reports
    # a non-zero remainder forever. --repaired says "I already ran the easy path
    # on this one"; the readable-partition-table check below is what actually
    # proves it, and it still has to pass.
    say "size % 512 == $R ($((R/32)) pass(es)) - --repaired given, verifying the partition table instead"
else
    die "size % 512 == $R -> this disk WAS encrypted ($((R/32)) pass(es)). Repair it with recover-easy-path.sh first, then re-run with --repaired."
fi

# belt and braces: the on-disk partition table must actually be readable
# A flat file held open by a powered-on VM (e.g. the batch rescue VM) is
# VMFS-locked: dd returns nothing, od errors, and the checks below would look
# like "no partition table". Detect that explicitly rather than blaming the disk.
if [ "$(dd if="$FLAT" bs=512 count=1 2>/dev/null | wc -c)" -lt 512 ]; then
    die "cannot read $FLAT - it is VMFS-locked. Power off any VM that has this disk attached (the batch rescue VM) and retry."
fi

# NB: ESXi's busybox has no tr(1) - use awk, which is present.
MBR=$(dd if="$FLAT" bs=512 count=1 2>/dev/null | od -An -tx1 -j 510 -N 2 | awk '{printf "%s%s",$1,$2}')
LBA1=$(dd if="$FLAT" bs=512 skip=1 count=1 2>/dev/null | head -c 8)
if [ "$MBR" = "55aa" ] || [ "$LBA1" = "EFI PART" ]; then
    say "partition table intact (MBR=$MBR, LBA1='$LBA1')"
else
    die "no readable partition table (MBR=$MBR) - do not treat this as undamaged"
fi

DESC=$(ls "$DIR"/*-recovered.vmdk 2>/dev/null | head -1)
[ -n "$DESC" ] || die "no *-recovered.vmdk descriptor - run make-descriptors.sh first"
say "descriptor: $(basename "$DESC")"

VMX=$(ls "$DIR"/*.vmx 2>/dev/null | head -1)
[ -n "$VMX" ] || die "no .vmx - this VM needs a new shell built around the disk"
say "vmx: $(basename "$VMX")"

# ------------------------------------------------ what needs changing in the vmx
# Disk node key, e.g. "nvme0:0" / "scsi0:0" / "sata0:0".
# Pick by DEVICE TYPE, never by position: a CD-ROM also has a .fileName, and on
# VMs where the cdrom line happens to come first, taking head -1 writes the disk
# descriptor onto the CD-ROM and leaves the real disk pointing at the dead
# encrypted descriptor -> "File <name>.vmdk was not found" on power on.
NODE=""
for cand in $(grep -oE '^(nvme|scsi|sata|ide)[0-9]+:[0-9]+\.fileName' "$VMX" | sed 's/\.fileName//' | sort -u); do
    dt=$(grep -E "^${cand}\.deviceType" "$VMX" | head -1 | sed 's/.*"\(.*\)"/\1/')
    fn=$(grep -E "^${cand}\.fileName"   "$VMX" | head -1 | sed 's/.*"\(.*\)"/\1/')
    case "$dt" in *cdrom*|*atapi*|*CDROM*) continue ;; esac
    case "$fn" in *.iso|*.ISO) continue ;; esac
    case "$fn" in *.vmdk) NODE="$cand"; break ;; esac
done
[ -n "$NODE" ] || die "could not find a hard-disk node in the vmx"
# keep the original MAC: guests pin it via netplan "match: macaddress:"
MAC=$(grep -iE '^ethernet0\.(generatedAddress|address) ' "$VMX" | head -1 | sed 's/.*"\(.*\)"/\1/')
FW=$(grep -iE '^firmware' "$VMX" | sed 's/.*"\(.*\)"/\1/')
say "disk node : $NODE  -> $(basename "$DESC")"
say "MAC       : ${MAC:-<none found>}  (pinned static)"
say "firmware  : ${FW:-bios}"
say "portgroup : $PG"

if [ "$COMMIT" != "--commit" ]; then
    echo
    say "DRY RUN - re-run with --commit to apply."
    exit 0
fi

# ---------------------------------------------------------------- rewrite vmx
cp "$VMX" "$VMX.bak-prerecover" || die "could not back up the vmx"
say "backed up: $(basename "$VMX").bak-prerecover"

# NB: "ethernet0\.address " needs the trailing space - without it the pattern
# ethernet0\.addressType matches only the Type key and a VM that already used a
# bare ethernet0.address ends up with TWO of them after we append ours.
# migrate.* + the .hlog are vMotion leftovers; if they survive, ESXi thinks the
# VM is mid-migration and power on fails with vim.fault.InvalidState.
grep -vE "^(${NODE}\.fileName|ethernet0\.dvs\.|ethernet0\.networkName|ethernet0\.addressType|ethernet0\.address |ethernet0\.generatedAddress|ethernet0\.generatedAddressOffset|migrate\.)" \
    "$VMX" > /tmp/_vmx.$$ || die "vmx filter failed"
{
  echo "${NODE}.fileName = \"$(basename "$DESC")\""
  echo "ethernet0.networkName = \"$PG\""
  if [ -n "$MAC" ]; then
    echo "ethernet0.addressType = \"static\""
    echo "ethernet0.address = \"$MAC\""
  else
    echo "ethernet0.addressType = \"generated\""
  fi
} >> /tmp/_vmx.$$
cat /tmp/_vmx.$$ > "$VMX"      # preserve inode + permissions
rm -f /tmp/_vmx.$$

# ------------------------------------------------------------ register + boot
VMID=$(vim-cmd vmsvc/getallvms 2>/dev/null | awk -v p="$VMX" '$0 ~ p {print $1}' | head -1)
if [ -z "$VMID" ]; then
    VMID=$(vim-cmd solo/registervm "$VMX" 2>&1 | sed 's/[^0-9]//g')
    case "$VMID" in ''|*[!0-9]*) die "register failed: $VMID";; esac
    say "registered as vmid $VMID"
else
    vim-cmd vmsvc/reload "$VMID" >/dev/null 2>&1
    say "already registered as vmid $VMID (reloaded)"
fi

vim-cmd vmsvc/power.on "$VMID" 2>&1 | sed 's/^/  /'
sleep 6
say "state: $(vim-cmd vmsvc/power.getstate "$VMID" 2>/dev/null | tail -1)"
echo
