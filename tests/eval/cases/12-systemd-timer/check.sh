#!/bin/bash
t=$(systemctl list-units --type=timer --all --no-legend | awk '{print $1}' | grep -i backup | head -1)
[ -n "$t" ] && systemctl is-active --quiet "$t" && systemctl is-enabled --quiet "$t" && systemctl cat "$t" | grep -qiE 'OnUnitActiveSec=10m|OnCalendar=.*/10'
