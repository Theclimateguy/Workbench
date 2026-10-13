#!/bin/bash
systemctl is-active --quiet cache-warm || exit 1
p1=$(systemctl show -p MainPID --value cache-warm); sleep 6
p2=$(systemctl show -p MainPID --value cache-warm)
systemctl is-active --quiet cache-warm && [ "$p1" = "$p2" ] && [ "$p1" != 0 ]
