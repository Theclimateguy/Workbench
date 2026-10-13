#!/bin/bash
sleep 3
systemctl is-active --quiet appwriter && [ "$(systemctl show -p User --value appwriter)" = app ] && [ "$(( $(date +%s) - $(cat /srv/app/data/heartbeat 2>/dev/null || echo 0) ))" -lt 10 ]
