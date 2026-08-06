#!/bin/sh
# Clear VMs stuck in a SUSPENDED state whose memory image was encrypted.
#
#   sh fix-suspended-vms.sh [--commit]
#
# A VM suspended when the encryptor ran has a .vmss that points at a .vmem which
# is now .vmem.babyk. Resuming fails with "Could not find the file ... an error
# caused the resume operation to fail", and ESXi asks Preserve or Discard.
#
# Discard is the only useful answer: the memory image is ciphertext and cannot be
# restored, so preserving it just leaves the VM unbootable. Discarding gives a
# clean cold boot from the (repaired) disk. The .vmss is moved aside rather than
# deleted, in case it is ever wanted for forensics.
COMMIT="${1:-}"
say() { echo "  $*"; }

vim-cmd vmsvc/getallvms 2>/dev/null | tail -n +2 | while read -r line; do
  VMID=$(echo "$line" | awk '{print $1}')
  case "$VMID" in ''|*[!0-9]*) continue;; esac
  ST=$(vim-cmd vmsvc/power.getstate "$VMID" 2>/dev/null | tail -1)
  case "$ST" in *Suspended*) ;; *) continue;; esac

  NAME=$(vim-cmd vmsvc/get.summary "$VMID" 2>/dev/null | grep -m1 'name = ' | sed 's/.*= "\(.*\)",/\1/')
  VMXP=$(vim-cmd vmsvc/get.summary "$VMID" 2>/dev/null | grep -m1 'vmPathName' | sed 's/.*= "\(.*\)",/\1/')
  DIR=$(echo "$VMXP" | sed 's/^\[\(.*\)\] \(.*\)\/[^/]*$/\/vmfs\/volumes\/\1\/\2/')

  echo "============================================================"
  echo "### $NAME (vmid $VMID) - SUSPENDED"
  say "folder: $DIR"
  ls "$DIR" 2>/dev/null | grep -iE 'vmss|vmem' | sed 's/^/     /'

  if [ "$COMMIT" != "--commit" ]; then
      say "DRY RUN - would power off (discarding suspend state), stash .vmss, power on"
      continue
  fi

  say "powering off to discard the unusable suspended state..."
  vim-cmd vmsvc/power.off "$VMID" 2>&1 | sed 's/^/     /'
  sleep 6
  say "state: $(vim-cmd vmsvc/power.getstate "$VMID" 2>/dev/null | tail -1)"

  for s in "$DIR"/*.vmss; do
      [ -f "$s" ] || continue
      mv "$s" "$s.discarded" && say "stashed $(basename "$s") -> $(basename "$s").discarded"
  done

  vim-cmd vmsvc/reload "$VMID" >/dev/null 2>&1
  say "powering on (cold boot from disk)..."
  vim-cmd vmsvc/power.on "$VMID" 2>&1 | sed 's/^/     /'
  sleep 10
  say "state: $(vim-cmd vmsvc/power.getstate "$VMID" 2>/dev/null | tail -1)"
done
echo
echo "done."
