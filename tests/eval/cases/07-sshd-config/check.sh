#!/bin/bash
sshd -t 2>/dev/null && systemctl is-active --quiet ssh && grep -q PermitRootLogin /etc/ssh/sshd_config.d/99-site.conf
