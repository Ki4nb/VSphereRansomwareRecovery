#!/bin/sh
# Create a SystemRescue VM with a damaged disk attached, ready for repair.
# Runs ON the ESXi host.
#
#   sh make-rescue-vm.sh <vm-folder> <portgroup> [--commit]
#   sh make-rescue-vm.sh ubuntu-01 "VM Network" --commit
#
# The disk is attached Independent-NONPERSISTENT, so every write the rescue OS
# makes is thrown away at power off and the original flat file cannot be
# damaged. Verify the repair first, then flip to Dependent and re-run for real:
#
#   sed -i '/^nvme0:0.mode/d' <rescue>.vmx   # -> Dependent
#   vim-cmd vmsvc/reload <vmid>
#
# The rescue VM is disposable: unregister it and delete its folder when done.

NAME="${1:?usage: make-rescue-vm.sh <vm-folder> <portgroup> [--commit] [--dependent]}"
PG="${2:?missing portgroup}"
COMMIT=""
DEPENDENT=""
for a in "$@"; do
  case "$a" in
    --commit)    COMMIT=1 ;;
    --dependent) DEPENDENT=1 ;;
  esac
done
MEM=4096
CPUS=2

# --dependent attaches the disk in normal (persistent) mode from the start.
# Use it once the procedure is proven on this layout: it saves a power-cycle
# and, because SystemRescue runs from RAM, a console trip to re-set the rescue
# VM's IP and root password. Until then, leave it off and rehearse first.
if [ -n "$DEPENDENT" ]; then
    DISKMODE=""
else
    DISKMODE='nvme0:0.mode = "independent-nonpersistent"'
fi

say() { echo "  $*"; }
die() { echo "  ABORT: $*"; exit 1; }

# ------------------------------------------------------------------ locate
DIR=""
for v in /vmfs/volumes/*; do
  [ -d "$v" ] && [ ! -L "$v" ] || continue
  [ -d "$v/$NAME" ] && DIR="$v/$NAME" && break
done
[ -n "$DIR" ] || die "VM folder '$NAME' not found"
VOL=$(dirname "$DIR")

DESC=$(ls "$DIR"/*-recovered.vmdk 2>/dev/null | head -1)
[ -n "$DESC" ] || die "no *-recovered.vmdk in $DIR - run make-descriptors.sh first"

ISO=$(find /vmfs/volumes -maxdepth 3 -iname 'systemrescue*.iso' 2>/dev/null | head -1)
[ -n "$ISO" ] || die "no systemrescue*.iso found under /vmfs/volumes"

RNAME="RESCUE-$NAME"
RDIR="$VOL/$RNAME"

echo "=== rescue VM for $NAME ==="
say "disk      : $DESC"
say "iso       : $ISO"
say "new VM    : $RDIR"
say "portgroup : $PG"
say "spec      : ${CPUS} vCPU, ${MEM} MB, no disk of its own"
say "disk mode : $([ -n "$DEPENDENT" ] && echo 'DEPENDENT - writes are PERMANENT' || echo 'independent-nonpersistent - writes discarded at power off')"

[ -d "$RDIR" ] && die "$RDIR already exists - delete it first"

if [ "$COMMIT" != "--commit" ]; then
    echo; say "DRY RUN - re-run with --commit to create it."
    exit 0
fi

mkdir -p "$RDIR" || die "mkdir failed"
VMX="$RDIR/$RNAME.vmx"

# EFI is the safe default: SystemRescue boots either way, and it matches the
# firmware of the Ubuntu guests we are repairing.
cat > "$VMX" << EOF
.encoding = "UTF-8"
config.version = "8"
virtualHW.version = "20"
displayName = "$RNAME"
guestOS = "other6xlinux-64"
firmware = "efi"
numvcpus = "$CPUS"
memSize = "$MEM"

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

floppy0.present = "FALSE"

sata0.present = "TRUE"
sata0:0.present = "TRUE"
sata0:0.deviceType = "cdrom-image"
sata0:0.fileName = "$ISO"
sata0:0.startConnected = "TRUE"

nvme0.present = "TRUE"
nvme0:0.present = "TRUE"
nvme0:0.fileName = "$DESC"
$DISKMODE

ethernet0.present = "TRUE"
ethernet0.virtualDev = "vmxnet3"
ethernet0.networkName = "$PG"
ethernet0.addressType = "generated"
ethernet0.startConnected = "TRUE"
EOF

say "wrote $(basename "$VMX")"

VMID=$(vim-cmd solo/registervm "$VMX" 2>&1 | sed 's/[^0-9]//g')
case "$VMID" in ''|*[!0-9]*) die "register failed: $VMID";; esac
say "registered as vmid $VMID"

vim-cmd vmsvc/power.on "$VMID" 2>&1 | sed 's/^/  /'
sleep 6
say "state: $(vim-cmd vmsvc/power.getstate "$VMID" 2>/dev/null | tail -1)"
echo
say "NEXT: open the console for vmid $VMID and run ALL of these:"
say "    systemctl disable --now iptables && systemctl start sshd   # 'stop' alone does not stick"
say "    passwd"
say "    ip -br link                                       # interface name varies per VM"
say "    ip addr add <free-ip>/<prefix> dev <iface> && ip route add default via <gw>"
say "(SystemRescue runs from RAM - ALL of this is lost on every reboot.)"
