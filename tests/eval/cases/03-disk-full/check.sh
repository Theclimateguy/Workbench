#!/bin/bash
use=$(df --output=pcent /var/log | tail -1 | tr -dc 0-9)
[ "$use" -lt 50 ] || { echo "/var/log still ${use}% used"; exit 1; }
# the service must still run, but not be filling the disk at the old rate (debug off or logrotate/limit in place)
systemctl is-active --quiet chatty || systemctl is-enabled chatty 2>/dev/null | grep -q disabled
