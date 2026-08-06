#!/bin/sh
# Authoritative "what is left" report, straight from the datastores.
#
# Does NOT depend on the fleet CSV: babuk_fleetscan.py cannot open a flat file
# that a powered-on VM holds a VMFS lock on, so it silently omits every disk
# belonging to a running VM - which, mid-recovery, is exactly the ones you have
# already fixed. This walks the filesystem instead and cross-checks against the
# registered/powered-on VM list.

REG=/tmp/_reg.$$
vim-cmd vmsvc/getallvms 2>/dev/null | tail -n +2 > "$REG"

printf "%-42s %7s %-18s %-9s %s\n" "VM FOLDER" "GiB" "STATE" "PASSES" "ACTION"
printf "%-42s %7s %-18s %-9s %s\n" "------------------------------------------" "-------" "------------------" "---------" "------"

TOT=0; DONE=0; A=0; B=0; C=0; SK=0
for v in /vmfs/volumes/*; do
  [ -d "$v" ] && [ ! -L "$v" ] || continue
  for d in "$v"/*/; do
    [ -d "$d" ] || continue
    n=$(basename "$d")
    f=$(ls "$d"*-flat.vmdk.babyk 2>/dev/null | head -1)
    [ -n "$f" ] || continue
    TOT=$((TOT+1))

    S=$(ls -l "$f" | awk '{print $5}')
    R=$((S % 512))
    GB=$((S / 1073741824))

    # Is it registered, and powered on?  getallvms prints "[datastore] FOLDER/x.vmx"
    # with no leading slash, and folder names may contain spaces, so match the
    # literal "] FOLDER/" substring rather than splitting into fields.
    VMID=$(awk -v pat="] $n/" 'index($0, pat) {print $1; exit}' "$REG")
    if [ -n "$VMID" ]; then
      PS=$(vim-cmd vmsvc/power.getstate "$VMID" 2>/dev/null | tail -1)
      case "$PS" in *"Powered on"*) STATE="RUNNING (vmid $VMID)";; *) STATE="registered off";; esac
    else
      STATE="not registered"
    fi

    # A powered-on VM holds a VMFS lock on its flat file, so every dd below
    # returns nothing and the disk would be misclassified. Decide on state first.
    if [ "$STATE" != "${STATE#RUNNING}" ]; then
        DONE=$((DONE+1))
        printf "%-42s %7s %-18s %-9s %s\n" "$n" "$GB" "$STATE" "$R" "** DONE **"
        continue
    fi

    # readable partition table = either untouched or already repaired
    MBR=$(dd if="$f" bs=512 count=1 2>/dev/null | od -An -tx1 -j 510 -N 2 | awk '{printf "%s%s",$1,$2}')
    LBA1=$(dd if="$f" bs=512 skip=1 count=1 2>/dev/null | head -c 8)
    OK=no
    [ "$MBR" = "55aa" ] && OK=yes
    [ "$LBA1" = "EFI PART" ] && OK=yes

    case "$n" in vCLS-*) ACT="skip (disposable vCLS agent)"; SK=$((SK+1));;
    *)
      if [ "$R" -eq 0 ]; then
          ACT="A: bringup-recovered-vm.sh"; A=$((A+1))
      elif [ "$OK" = "yes" ]; then
          ACT="A: REPAIRED -> bringup --repaired"; A=$((A+1))
      elif [ "$LBA1" = "EFI PART" ] || [ "$MBR" = "55aa" ]; then
          ACT="B: easy path"; B=$((B+1))
      else
          # damaged head: GPT-based guests take the easy path, MBR-only ones need testdisk
          SZ=$((S - R))
          BK=$(dd if="$f" bs=512 skip=$(( SZ/512 - 1 )) count=1 2>/dev/null | head -c 8 2>/dev/null)
          if [ "$BK" = "EFI PART" ]; then ACT="B: easy path"; B=$((B+1))
          else ACT="C: MBR - testdisk"; C=$((C+1)); fi
      fi;;
    esac

    case "$STATE" in RUNNING*) ACT="** DONE **"; DONE=$((DONE+1));; esac
    printf "%-42s %7s %-18s %-9s %s\n" "$n" "$GB" "$STATE" "$R" "$ACT"
  done
done
rm -f "$REG"

echo
echo "flat disks total : $TOT"
echo "already running  : $DONE"
echo "A (descriptor)   : $A"
echo "B (easy path)    : $B"
echo "C (testdisk/MBR) : $C"
echo "skip             : $SK"
