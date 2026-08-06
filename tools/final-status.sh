#!/bin/sh
# Final per-VM status report for a recovered ESXi host.
printf "%-34s %-6s %-12s %-16s %s\n" "VM" "VMID" "POWER" "TOOLS" "IP"
printf "%-34s %-6s %-12s %-16s %s\n" "----------------------------------" "------" "------------" "----------------" "--"
ON=0; OFF=0; TOOLS=0
for id in $(vim-cmd vmsvc/getallvms 2>/dev/null | tail -n +2 | awk '{print $1}'); do
  case "$id" in ''|*[!0-9]*) continue;; esac
  N=$(vim-cmd vmsvc/get.summary "$id" 2>/dev/null | grep -m1 'name = ' | sed 's/.*= "\(.*\)",/\1/')
  P=$(vim-cmd vmsvc/power.getstate "$id" 2>/dev/null | tail -1)
  case "$P" in *"Powered on"*) P=on; ON=$((ON+1));; *"Powered off"*) P=off; OFF=$((OFF+1));; *) P=other;; esac
  G=$(vim-cmd vmsvc/get.guest "$id" 2>/dev/null)
  T=$(echo "$G" | grep -m1 'toolsStatus' | sed 's/.*= "\(.*\)",/\1/')
  case "$T" in toolsOk) TOOLS=$((TOOLS+1));; esac
  I=$(echo "$G" | grep -m1 '   ipAddress = "' | sed 's/.*= "\(.*\)",/\1/')
  printf "%-34s %-6s %-12s %-16s %s\n" "$N" "$id" "$P" "${T:-?}" "${I:-}"
done
echo
echo "powered on: $ON   off: $OFF   tools reporting: $TOOLS"
