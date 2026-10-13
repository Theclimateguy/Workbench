#!/bin/bash
cat > /etc/ssh/sshd_config.d/99-site.conf <<'C'
# site hardening (draft)
PermitRootLogin no
MaxAuthTries three
Ciphers aes256-gcm@openssh.com,chacha20-poly1305@openssh.com
C
systemctl reload ssh 2>/dev/null || true
