#!/usr/bin/env bash
# Runs ON THE WORKSTATION (as root) to let the Spark support agent in.
# Usage: sudo bash onboard-target.sh "<agent public key>"
# Creates user support-agent (groups adm, sudo) with key-only SSH and passwordless sudo,
# so the agent can read logs and, after the user approves each change, fix things.
set -euo pipefail
PUBKEY=${1:-}; [ -n "$PUBKEY" ] || { echo "usage: $0 '<ssh public key>'" >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

id support-agent >/dev/null 2>&1 || useradd -m -s /bin/bash -G adm,sudo support-agent
install -d -m 700 -o support-agent -g support-agent /home/support-agent/.ssh
grep -qxF "$PUBKEY" /home/support-agent/.ssh/authorized_keys 2>/dev/null || echo "$PUBKEY" >> /home/support-agent/.ssh/authorized_keys
chmod 600 /home/support-agent/.ssh/authorized_keys; chown support-agent:support-agent /home/support-agent/.ssh/authorized_keys
passwd -l support-agent >/dev/null
echo "support-agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/90-support-agent; chmod 440 /etc/sudoers.d/90-support-agent
# Harden sshd for this account: keys only.
install -d /etc/ssh/sshd_config.d
printf 'Match User support-agent\n    PasswordAuthentication no\n    PubkeyAuthentication yes\n' > /etc/ssh/sshd_config.d/90-support-agent.conf
sshd -t && (systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true)
echo "support-agent ready on $(hostname): $(ip -br addr | awk '$2=="UP"{print $3}' | tr '\n' ' ')"
