#!/bin/bash
# Drive recover-easy-path.sh across every disk attached to the batch rescue VM.
# Runs INSIDE the rescue VM.
#
#   bash batch-repair.sh /tmp/manifest.txt            # dry run every disk
#   bash batch-repair.sh /tmp/manifest.txt --commit   # repair every disk
#
# manifest.txt lines:  <controller> <unit> <original_bytes> <passes> <vm name>
#
# Mapping /dev/sdX -> VM is the hard part: these guests are clones, so they share
# filesystem UUIDs, VG names and LVM UUIDs, and most are the same size. We map by
# SCSI address instead. Linux host numbers do NOT equal vmx controller numbers
# (the AHCI cdrom controller also claims one), so we match each Linux host to a
# manifest controller by comparing its full set of "unit:size" pairs, then verify
# each disk's byte size before touching it.

set -u
MAN="${1:?usage: batch-repair.sh <manifest> [--commit]}"
COMMIT="${2:-}"
REC=/tmp/rec.sh
[ -f "$REC" ] || { echo "missing $REC (push recover-easy-path.sh first)"; exit 1; }

echo "=== attached SCSI disks ==="
lsblk -S -o NAME,HCTL,SIZE,VENDOR,MODEL 2>/dev/null | head -50
echo

# ---- build "host -> signature" from Linux, and "controller -> signature" from the manifest
declare -A LSIG LDEV MSIG
while read -r name hctl; do
    [ -n "$name" ] || continue
    h=${hctl%%:*}; rest=${hctl#*:}; rest=${rest#*:}; t=${rest%%:*}
    b=$(blockdev --getsize64 "/dev/$name" 2>/dev/null)
    [ -n "$b" ] || continue
    LSIG[$h]+="$t:$b "
    LDEV[$h:$t]="/dev/$name"
done < <(lsblk -S -n -o NAME,HCTL 2>/dev/null)

# "|| [ -n "$c" ]" is required: if the manifest has no trailing newline, plain
# `read` returns false on the final record and silently drops it - which also
# corrupts the controller signature below and makes a whole controller unmatched.
while read -r c u bytes passes name || [ -n "${c:-}" ]; do
    [ -n "${c:-}" ] || continue
    MSIG[$c]+="$u:$bytes "
done < "$MAN"

norm() { tr ' ' '\n' <<<"$1" | grep -v '^$' | sort | tr '\n' ' '; }

declare -A H2C
for h in "${!LSIG[@]}"; do
    ls_=$(norm "${LSIG[$h]}")
    for c in "${!MSIG[@]}"; do
        [ "$(norm "${MSIG[$c]}")" = "$ls_" ] && { H2C[$h]=$c; break; }
    done
done

echo "=== host -> vmx controller mapping ==="
if [ ${#H2C[@]} -eq 0 ]; then
    echo "  FAILED to match any Linux host to a manifest controller."
    echo "  Refusing to guess - a wrong map would repair the wrong disk."
    exit 1
fi
for h in "${!H2C[@]}"; do echo "  host$h -> scsi${H2C[$h]}"; done
echo

OK=0; FAIL=0; SKIP=0
while read -r c u bytes passes name || [ -n "${c:-}" ]; do
    [ -n "${c:-}" ] || continue
    dev=""
    for h in "${!H2C[@]}"; do
        [ "${H2C[$h]}" = "$c" ] && dev="${LDEV[$h:$u]:-}" && break
    done
    echo "############################################################"
    case "$name" in
        '#SKIP#'*) echo "### scsi$c:$u  ${name#\#SKIP\#}"
                   echo "  SKIPPED by request"; SKIP=$((SKIP+1)); continue ;;
    esac
    echo "### scsi$c:$u  $name"
    if [ -z "$dev" ]; then
        echo "  SKIP: no Linux device found for scsi$c:$u"; SKIP=$((SKIP+1)); continue
    fi
    actual=$(blockdev --getsize64 "$dev")
    if [ "$actual" != "$bytes" ]; then
        echo "  SKIP: $dev is $actual bytes, manifest says $bytes - mapping is wrong, not touching it"
        SKIP=$((SKIP+1)); continue
    fi
    # Resume: a previous run that died mid-way leaves completed logs behind.
    # Re-running a finished disk is harmless but slow, so skip proven-good ones.
    if [ -f "/tmp/log.$c.$u" ] && grep -q '=== DONE ===' "/tmp/log.$c.$u" 2>/dev/null; then
        echo "  already completed in an earlier run - skipping"; OK=$((OK+1)); continue
    fi
    echo "  device: $dev  ($((bytes/1073741824)) GiB, $passes pass(es)) - size verified"
    if bash "$REC" $COMMIT "$dev" > /tmp/log.$c.$u 2>&1; then
        tail -4 /tmp/log.$c.$u | sed 's/^/    /'
        echo "  RESULT: OK"; OK=$((OK+1))
    else
        echo "  RESULT: FAILED"
        grep -E 'ABORT|error|Error|failed' /tmp/log.$c.$u | head -5 | sed 's/^/    /'
        FAIL=$((FAIL+1))
    fi
done < "$MAN"

echo
echo "============================================================"
echo " ok: $OK   failed: $FAIL   skipped: $SKIP"
echo " per-disk logs in /tmp/log.<controller>.<unit>"
