#!/bin/sh
# Bring recovered VMs up ONE AT A TIME, waiting for each to finish booting before
# starting the next. Runs ON the ESXi host.
#
#   sh bringup-sequential.sh <portgroup> --list FILE [--commit] [--wait N] [--grace N]
#
# FILE holds one VM folder name per line (blank lines and #comments ignored).
#
# Starting thirty repaired VMs at once on a host that has just been through an
# incident is a good way to turn a recovery into an outage: they contend for the
# same datastore, and if one of them is going to fail its fsck or hang on a
# missing mount you want to see that before the next twenty-nine follow it.
#
# "Booted" means VMware Tools reports running. Guests without Tools installed can
# never report, so after --wait seconds this falls back to the VM's power state,
# warns, and moves on rather than stalling the whole queue.
#
# Requires bringup-recovered-vm.sh alongside it (same directory, or /tmp).

PG="${1:?usage: bringup-sequential.sh <portgroup> --list FILE [--commit] [--wait N] [--grace N]}"
shift
COMMIT=""; WAIT=300; GRACE=30; LISTFILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --commit) COMMIT="--commit" ;;
    --list)   shift; LISTFILE="$1" ;;
    --wait)   shift; WAIT="$1" ;;
    --grace)  shift; GRACE="$1" ;;
  esac
  shift
done

[ -n "$LISTFILE" ] || { echo "ABORT: --list FILE is required"; exit 1; }
[ -f "$LISTFILE" ] || { echo "ABORT: list file '$LISTFILE' not found"; exit 1; }
VMS=$(grep -v '^[[:space:]]*$' "$LISTFILE" | grep -v '^[[:space:]]*#')

BRINGUP=""
for c in "$(dirname "$0")/bringup-recovered-vm.sh" /tmp/bringup-recovered-vm.sh; do
  [ -f "$c" ] && BRINGUP="$c" && break
done
[ -n "$BRINGUP" ] || { echo "ABORT: bringup-recovered-vm.sh not found"; exit 1; }

# getallvms prints "[datastore] FOLDER/x.vmx" with no leading slash, and folder
# names may contain spaces, so match the literal "] FOLDER/" substring rather
# than splitting into fields.
vmid_of() {
    vim-cmd vmsvc/getallvms 2>/dev/null | awk -v pat="] $1/" 'index($0,pat){print $1; exit}'
}

UP=0; NOVMX=0; FAILED=0; NOTOOLS=0
OLDIFS=$IFS; IFS='
'
for n in $VMS; do
  [ -n "$n" ] || continue
  echo
  echo "================================================================"
  echo ">>> $n"
  echo "================================================================"

  EX=$(vmid_of "$n")
  if [ -n "$EX" ] && vim-cmd vmsvc/power.getstate "$EX" 2>/dev/null | grep -q "Powered on"; then
      echo "  already powered on (vmid $EX) - skipping"; UP=$((UP+1)); continue
  fi

  OUT=$(sh "$BRINGUP" "$n" "$PG" --repaired $COMMIT 2>&1)
  echo "$OUT" | sed 's/^/  /'

  case "$OUT" in
    *"needs a new shell"*) echo "  >> NO .vmx - cannot boot until a VM shell is built"; NOVMX=$((NOVMX+1)); continue ;;
    *"VMFS-locked"*)       echo "  >> DISK LOCKED - power off the rescue VM first"; FAILED=$((FAILED+1)); continue ;;
    *ABORT*)               echo "  >> FAILED"; FAILED=$((FAILED+1)); continue ;;
  esac
  [ -n "$COMMIT" ] || continue

  VMID=$(vmid_of "$n")
  [ -n "$VMID" ] || { echo "  >> could not resolve vmid"; FAILED=$((FAILED+1)); continue; }

  echo "  waiting up to ${WAIT}s for guest to finish booting..."
  T=0; BOOTED=""
  while [ "$T" -lt "$WAIT" ]; do
      G=$(vim-cmd vmsvc/get.guest "$VMID" 2>/dev/null)
      case "$G" in *guestToolsRunning*) BOOTED=tools; break;; esac
      sleep 10; T=$((T+10))
  done

  if [ "$BOOTED" = "tools" ]; then
      IP=$(vim-cmd vmsvc/get.guest "$VMID" 2>/dev/null | grep -m1 '   ipAddress = "' | sed 's/.*= "\(.*\)",/\1/')
      echo "  BOOTED after ${T}s  (vmid $VMID)  ip=${IP:-<none reported yet>}"
      UP=$((UP+1))
  else
      PS=$(vim-cmd vmsvc/power.getstate "$VMID" 2>/dev/null | tail -1)
      echo "  no Tools heartbeat after ${WAIT}s - power state: $PS"
      echo "  (guest may simply not have open-vm-tools installed)"
      NOTOOLS=$((NOTOOLS+1))
  fi

  echo "  grace ${GRACE}s before the next VM..."
  sleep "$GRACE"
done
IFS=$OLDIFS

echo
echo "================================================================"
echo " booted (Tools confirmed) : $UP"
echo " started, no Tools report : $NOTOOLS"
echo " no .vmx (need a shell)   : $NOVMX"
echo " failed                   : $FAILED"
echo "================================================================"
