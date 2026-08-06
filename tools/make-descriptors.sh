#!/bin/sh
# Generate attachable VMDK descriptors for Babuk-damaged flat files.
#
# Pure POSIX shell - runs on any ESXi host with no Python and nothing uploaded.
# Paste it into an SSH session, or save and `sh make-descriptors.sh`.
#
# NON-DESTRUCTIVE: only ever CREATES new "<name>-recovered.vmdk" files.
# It never renames, overwrites, or writes one byte to the flat data.
#
# Usage:
#   sh make-descriptors.sh                 # dry run over every mounted datastore
#   sh make-descriptors.sh --write         # actually create the descriptors
#   sh make-descriptors.sh --write /vmfs/volumes/<uuid>    # limit to one volume

MODE=dry
VOLS=""
for a in "$@"; do
  case "$a" in
    --write) MODE=write ;;
    *) VOLS="$VOLS $a" ;;
  esac
done

# Default: every real (non-symlink) volume under /vmfs/volumes
if [ -z "$VOLS" ]; then
  for d in /vmfs/volumes/*; do
    [ -d "$d" ] && [ ! -L "$d" ] && VOLS="$VOLS $d"
  done
fi

mkdesc() {
  f="$1"
  b=$(basename "$f")
  d=$(dirname "$f")

  # exact byte size
  S=$(ls -l "$f" | awk '{print $5}')

  # VMFS flat files are always 512-aligned, so the remainder is the number of
  # appended 32-byte ephemeral keys: 0 = untouched, 32 = one pass, 64 = two.
  R=$((S % 512))
  P=0
  [ "$R" -eq 32 ] && P=32
  [ "$R" -eq 64 ] && P=64
  if [ "$R" -ne 0 ] && [ "$P" -eq 0 ]; then
    echo "  SKIP (odd padding $R): $b"
    return 1
  fi

  O=$((S - P))          # original size, key bytes excluded
  SEC=$((O / 512))      # extent length in sectors -> also restores alignment
  CYL=$((SEC / 16065))  # 255 heads * 63 sectors
  [ "$CYL" -lt 1 ] && CYL=1

  name=${b%-flat.vmdk.babyk}
  out="$d/${name}-recovered.vmdk"

  if [ "$MODE" = "write" ]; then
    if [ -e "$out" ]; then
      echo "  EXISTS, not touching: ${name}-recovered.vmdk"
      return 0
    fi
    {
      echo "# Disk DescriptorFile"
      echo "version=1"
      echo "encoding=\"UTF-8\""
      echo "CID=fffffffe"
      echo "parentCID=ffffffff"
      echo "isNativeSnapshot=\"no\""
      echo "createType=\"vmfs\""
      echo ""
      echo "# Extent description"
      echo "RW $SEC VMFS \"$b\""
      echo ""
      echo "# The Disk Data Base"
      echo "#DDB"
      echo "ddb.adapterType = \"lsilogic\""
      echo "ddb.geometry.cylinders = \"$CYL\""
      echo "ddb.geometry.heads = \"255\""
      echo "ddb.geometry.sectors = \"63\""
      echo "ddb.virtualHWVersion = \"21\""
    } > "$out"
    echo "  CREATED ${name}-recovered.vmdk  ($SEC sectors, $((P / 32)) pass(es))"
  else
    echo "  DRY    ${name}-recovered.vmdk  ($SEC sectors, $((P / 32)) pass(es))"
  fi
}

echo "mode: $MODE"
echo "volumes:$VOLS"
echo

# NOTE: `for f in $(find ...)` would word-split on VM folders containing
# spaces (e.g. "192.0.2.90-prod app 04"). Read line-by-line instead.
TMP=/tmp/.mkdesc.list.$$
: > "$TMP"
for v in $VOLS; do
  find "$v" -type f -name "*-flat.vmdk.babyk" 2>/dev/null >> "$TMP"
done

N=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  mkdesc "$f" && N=$((N + 1))
done < "$TMP"
rm -f "$TMP"

echo
echo "descriptors handled: $N"
[ "$MODE" = "dry" ] && echo "re-run with --write to create them"
