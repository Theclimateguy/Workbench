#!/bin/bash
set -e
f=$(ls /etc/netplan/*.yaml | head -1)
cp "$f" "/root/.netplan-orig.yaml"
# YAML indentation error: netplan generate fails, networkd gets nothing
sed -i 's/^\(\s*\)dhcp4: true/\1 dhcp4: true/' "$f"
netplan apply 2>/dev/null || true
