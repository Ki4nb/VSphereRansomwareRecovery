#!/bin/sh
# Build ONE SystemRescue VM with many damaged disks attached at once, plus a
# manifest mapping each SCSI unit to a VM, so the repair loop inside the rescue
# VM knows which /dev/sdX belongs to which guest.
#
#   sh make-batch-rescue-vm.sh <portgroup> --list FILE [--commit] [--dependent]
#
# FILE holds one VM per line: either a bare folder name, or a full path when the
# same folder name exists on several datastores (a VM whose disks are spread
# across datastore1/2/3 will have three identically-named folders, and a bare
# name silently picks whichever sorts first).
#
#   ubuntu-01
#   ubuntu-02
#   /vmfs/volumes/<datastore-uuid>/app-server-01
#
# Why one rescue VM instead of one per disk: booting SystemRescue costs a console
# trip every time (it runs from RAM, so the root password, firewall state and IP
# are lost on every boot). Attaching thirty disks to a single rescue VM turns
# thirty console trips into one.
#
# --dependent attaches the disks in normal mode, so repairs are permanent
# immediately. Use it only once the layout is proven on that fleet; until then
# leave it off, rehearse in the default non-persistent mode, and confirm the
# result before committing.
#
# ESXi tops out around 60 disks per VM (4 pvscsi controllers x 15 usable units).
# Split larger fleets into batches.

PG="${1:?usage: make-batch-rescue-vm.sh <portgroup> --list FILE [--commit] [--dependent]}"
COMMIT=""; DEPENDENT=""; LISTFILE=""
PREV=""
for a in "$@"; do
  case "$PREV" in --list) LISTFILE="$a" ;; esac
  case "$a" in
    --commit)    COMMIT=1 ;;
    --dependent) DEPENDENT=1 ;;
  esac
  PREV="$a"
done

RNAME="RESCUE-BATCH"
MEM=8192
CPUS=4
PER_CTRL=15          # pvscsi units 0-6 and 8-15; unit 7 is reserved for the HBA

say() { echo "  $*"; }
die() { echo "  ABORT: $*"; exit 1; }

[ -n "$LISTFILE" ] || die "--list FILE is required (one VM folder name or path per line)"
[ -f "$LISTFILE" ] || die "list file '$LISTFILE' not found"
VMS=$(grep -v '^[[:space:]]*$' "$LISTFILE" | grep -v '^[[:space:]]*#')
[ -n "$VMS" ] || die "list file is empty"

if [ -n "$DEPENDENT" ]; then
    DISKMODE=""
else
    DISKMODE='SCSINODE.mode = "independent-nonpersistent"'
fi

ISO=$(find /vmfs/volumes -maxdepth 3 -iname 'systemrescue*.iso' 2>/dev/null | head -1)
[ -n "$ISO" ] || die "no systemrescue*.iso found under /vmfs/volumes - upload one first"

# put the rescue VM on the datastore with the most free space
VOL=$(df -m 2>/dev/null | awk '/vmfs.volumes/ {print $4" "$6}' | sort -rn | head -1 | cut -d' ' -f2-)
[ -n "$VOL" ] || VOL=$(dirname "$(dirname "$ISO")")
RDIR="$VOL/$RNAME"

echo "=== batch rescue VM ==="
say "iso       : $ISO"
say "location  : $RDIR"
say "portgroup : $PG"
say "spec      : ${CPUS} vCPU, ${MEM} MB, no disk of its own"
say "disk mode : $([ -n "$DEPENDENT" ] && echo 'DEPENDENT - writes are PERMANENT' || echo 'independent-nonpersistent - discarded at power off')"
echo

# ---------------------------------------------------------------- resolve disks
BODY=/tmp/_disks.$$;  MAN=/tmp/_man.$$
: > "$BODY"; : > "$MAN"
IDX=0; MISSING=0
OLDIFS=$IFS; IFS='
'
for n in $VMS; do
  [ -n "$n" ] || continue
  DIR=""
  case "$n" in
    /*) [ -d "$n" ] && DIR="$n" ;;
    *)  for v in /vmfs/volumes/*; do
          [ -d "$v" ] && [ ! -L "$v" ] || continue
          [ -d "$v/$n" ] && DIR="$v/$n" && break
        done ;;
  esac
  if [ -z "$DIR" ]; then echo "  MISSING FOLDER: $n"; MISSING=$((MISSING+1)); continue; fi
  n=$(basename "$DIR")

  DESC=$(ls "$DIR"/*-recovered.vmdk 2>/dev/null | head -1)
  if [ -z "$DESC" ]; then echo "  NO DESCRIPTOR : $n"; MISSING=$((MISSING+1)); continue; fi

  FLAT=$(ls "$DIR"/*-flat.vmdk.babyk 2>/dev/null | head -1)
  S=$(ls -l "$FLAT" | awk '{print $5}')
  R=$((S % 512)); O=$((S - R))

  C=$((IDX / PER_CTRL))
  U=$((IDX % PER_CTRL));  [ "$U" -ge 7 ] && U=$((U + 1))
  [ "$C" -le 3 ] || die "more than $((PER_CTRL*4)) disks - split into batches"

  {
    echo "scsi$C:$U.present = \"TRUE\""
    echo "scsi$C:$U.deviceType = \"scsi-hardDisk\""
    echo "scsi$C:$U.fileName = \"$DESC\""
    [ -n "$DEPENDENT" ] || echo "scsi$C:$U.mode = \"independent-nonpersistent\""
  } >> "$BODY"

  # manifest: controller unit original_bytes passes vm_name
  echo "$C $U $O $((R/32)) $n" >> "$MAN"
  printf "  scsi%s:%-2s  %-42s %6s GiB  %s pass(es)\n" "$C" "$U" "$n" "$((O/1073741824))" "$((R/32))"
  IDX=$((IDX+1))
done
IFS=$OLDIFS

echo
say "disks resolved: $IDX   missing: $MISSING"
[ "$IDX" -gt 0 ] || die "nothing to attach"

if [ -z "$COMMIT" ]; then
  echo; say "DRY RUN - re-run with --commit to create the VM."
  rm -f "$BODY" "$MAN"; exit 0
fi

[ -d "$RDIR" ] && die "$RDIR already exists - delete it first"
mkdir -p "$RDIR" || die "mkdir failed"
VMX="$RDIR/$RNAME.vmx"

cat > "$VMX" <<EOF
.encoding = "UTF-8"
config.version = "8"
virtualHW.version = "20"
displayName = "$RNAME"
guestOS = "other6xlinux-64"
firmware = "efi"
numvcpus = "$CPUS"
memSize = "$MEM"
floppy0.present = "FALSE"

pciBridge0.present = "TRUE"
pciBridge4.present = "TRUE"
pciBridge4.virtualDev = "pcieRootPort"
pciBridge4.functions = "8"
pciBridge5.present = "TRUE"
pciBridge5.virtualDev = "pcieRootPort"
pciBridge5.functions = "8"
pciBridge6.present = "TRUE"
pciBridge6.virtualDev = "pcieRootPort"
pciBridge6.functions = "8"
pciBridge7.present = "TRUE"
pciBridge7.virtualDev = "pcieRootPort"
pciBridge7.functions = "8"

sata0.present = "TRUE"
sata0:0.present = "TRUE"
sata0:0.deviceType = "cdrom-image"
sata0:0.fileName = "$ISO"
sata0:0.startConnected = "TRUE"

ethernet0.present = "TRUE"
ethernet0.virtualDev = "vmxnet3"
ethernet0.networkName = "$PG"
ethernet0.addressType = "generated"
ethernet0.startConnected = "TRUE"
EOF

C=0
while [ "$C" -le 3 ]; do
  if grep -q "^scsi$C:" "$BODY"; then
    echo "scsi$C.present = \"TRUE\""      >> "$VMX"
    echo "scsi$C.virtualDev = \"pvscsi\"" >> "$VMX"
  fi
  C=$((C+1))
done
cat "$BODY" >> "$VMX"

cp "$MAN" "$RDIR/manifest.txt"
rm -f "$BODY" "$MAN"
say "wrote $(basename "$VMX") and manifest.txt ($IDX disks)"

VMID=$(vim-cmd solo/registervm "$VMX" 2>&1 | sed 's/[^0-9]//g')
case "$VMID" in ''|*[!0-9]*) die "register failed: $VMID";; esac
say "registered as vmid $VMID"
vim-cmd vmsvc/power.on "$VMID" 2>&1 | sed 's/^/  /'
sleep 8
say "state: $(vim-cmd vmsvc/power.getstate "$VMID" 2>/dev/null | tail -1)"
echo
say "NEXT: on the console for vmid $VMID run ALL of:"
say "    systemctl disable --now iptables && systemctl start sshd   # 'stop' alone does not stick"
say "    passwd"
say "    ip -br link                                                # interface name varies"
say "    ip addr add <free-ip>/<prefix> dev <iface> && ip route add default via <gw>"
say "(SystemRescue runs from RAM - ALL of this is lost on every reboot.)"
