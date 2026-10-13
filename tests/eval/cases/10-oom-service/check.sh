#!/bin/bash
sleep 6
systemctl is-active --quiet cache-warm && [ "$(systemctl show -p NRestarts --value cache-warm)" -lt 3 ] 2>/dev/null || [ "$(systemctl show -p ActiveEnterTimestampMonotonic --value cache-warm)" != 0 ] && systemctl is-active --quiet cache-warm
