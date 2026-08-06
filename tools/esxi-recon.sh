#!/bin/sh
# READ-ONLY recon of an ESXi host hit by the same locker. Changes nothing.
echo "=== host ==="
uname -a
vmware -v
uptime
echo
echo "=== IOC check: is the encryptor still present? ==="
for f in /var/run/backup /var/run/run.sh /tmp/script_output_1.txt /tmp/script_output_2.txt /tmp/.m.out; do
  if [ -e "$f" ]; then echo "  PRESENT: $(ls -la $f)"; else echo "  absent : $f"; fi
done
echo
echo "-- execInstalledOnly (should be 1 = enforced) --"
esxcli system settings advanced list -o /User/execInstalledOnly 2>/dev/null | grep -E 'Int Value|Path'
echo
echo "=== datastores ==="
esxcli storage filesystem list 2>/dev/null
echo
echo "-- unresolved/snapshot VMFS volumes --"
esxcfg-volume -l 2>/dev/null || echo "  none"
echo
echo "=== registered VMs ==="
vim-cmd vmsvc/getallvms 2>&1 | head -60
echo
echo "=== encrypted files per datastore ==="
N=0
for d in /vmfs/volumes/*; do
  [ -d "$d" ] && [ ! -L "$d" ] || continue
  C=$(find "$d" -name "*.babyk" 2>/dev/null | wc -l)
  R=$(find "$d" -name "How To Restore*" 2>/dev/null | wc -l)
  echo "  $d : $C encrypted, $R ransom notes"
  N=$((N+C))
done
echo "TOTAL encrypted files: $N"
echo
echo "=== flat disks > 512 MiB, with pass count from size % 512 ==="
for d in /vmfs/volumes/*; do
  [ -d "$d" ] && [ ! -L "$d" ] || continue
  find "$d" -type f -name "*-flat.vmdk.babyk" 2>/dev/null
done > /tmp/_flats.txt
while IFS= read -r f; do
  [ -n "$f" ] || continue
  S=$(ls -l "$f" | awk '{print $5}')
  R=$((S % 512))
  case $R in
    0)  P="NEVER ENCRYPTED" ;;
    32) P="1 pass" ;;
    64) P="2 passes" ;;
    *)  P="odd padding $R" ;;
  esac
  GB=$((S / 1073741824))
  printf "  %-70s %6d GiB  %s\n" "$(basename "$f")" "$GB" "$P"
done < /tmp/_flats.txt
echo
echo "count: $(wc -l < /tmp/_flats.txt) flat disks"
echo
echo "=== existing -recovered.vmdk descriptors ==="
for d in /vmfs/volumes/*; do
  [ -d "$d" ] && [ ! -L "$d" ] || continue
  find "$d" -name "*-recovered.vmdk" 2>/dev/null
done | wc -l
