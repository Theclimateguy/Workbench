#!/usr/bin/env bash
# deploy.sh — copy the Workbench software onto this GX10 (idempotent; no downloads, no model handling).
# Used by bootstrap.sh for the first install and by `support-ctl update` afterwards.
#   agent/  -> /opt/support        (bin, mcp, ui, workspace, desktop, tests)
#   stack/  -> /opt/support-stack  (compose, env, systemd units, netplan profiles)
#   support-ctl -> /usr/local/bin; systemd units templated for the current user.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
SUPPORT_DIR=${SUPPORT_DIR:-/opt/support}
STACK_DIR=${STACK_DIR:-/opt/support-stack}
RUN_USER=${RUN_USER:-$(id -un)}
log() { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }

[ "$(id -u)" != 0 ] || { echo "run as the operator user (with sudo rights), not root" >&2; exit 1; }
sudo install -d -o "$RUN_USER" -g "$RUN_USER" "$SUPPORT_DIR" "$STACK_DIR" /opt/models /opt/build/logs /opt/build/hf-cache
sudo install -d -o "$RUN_USER" -g "$RUN_USER" -m 755 /var/log/support
sudo install -d -m 755 /etc/support

log "software -> $SUPPORT_DIR, $STACK_DIR"
# Never clobber per-device state: targets/, keys/, workspace/docs/ (operator's documentation).
rsync -a --delete \
  --exclude 'targets/' --exclude 'keys/' --exclude 'workspace/docs/' --exclude 'ui-assets/' --exclude 'manual/' --exclude 'venv/' --exclude '__pycache__/' \
  "$REPO/agent/" "$SUPPORT_DIR/"
rsync -a "$REPO/agent/workspace/docs/" "$SUPPORT_DIR/workspace/docs/" --ignore-existing
rsync -a "$REPO/tests/" "$SUPPORT_DIR/tests/"
rsync -a --delete "$REPO/stack/" "$STACK_DIR/"
chmod +x "$SUPPORT_DIR"/bin/*
sudo ln -sf "$SUPPORT_DIR/bin/support-ctl" /usr/local/bin/support-ctl

log "systemd units (user: $RUN_USER)"
for u in llm@.service support-opencode.service support-ui.service support-ui-tls.service; do
  sed -e "s|^User=admin|User=$RUN_USER|; s|^Group=admin|Group=$RUN_USER|" \
      -e "s|/home/admin/|/home/$RUN_USER/|g" "$STACK_DIR/systemd/$u" | sudo tee "/etc/systemd/system/$u" >/dev/null
done
sudo systemctl daemon-reload

log "network profiles for the RJ45 port (lan + direct fallback)"
RJ45=$(ip -o link | awk -F': ' '$2 ~ /^en/ && $2 !~ /docker|veth|br-/ {print $2}' | head -1)
# Skip entirely when the profiles already exist under any name (first device got them via nmcli) —
# a second copy with the same UUID would break netplan on the very link we may be deployed over.
if nmcli -t -f NAME con show 2>/dev/null | grep -qxE 'lan|direct'; then
  log "  lan/direct profiles already present, leaving NetworkManager alone"; RJ45=""
fi
for f in 90-support-lan.yaml 90-support-direct.yaml; do
  if [ -n "$RJ45" ] && ! sudo test -f "/etc/netplan/$f"; then
    sed "s|enP7s7|$RJ45|" "$STACK_DIR/net/$f" | sudo tee "/etc/netplan/$f" >/dev/null
    sudo chmod 600 "/etc/netplan/$f"
    NETPLAN_CHANGED=1
  fi
done
[ -n "${NETPLAN_CHANGED:-}" ] && { sudo netplan apply >/dev/null 2>&1 || log "netplan apply failed — check /etc/netplan"; }

if [ -d "$SUPPORT_DIR/venv" ]; then
  log "python deps"
  "$SUPPORT_DIR/venv/bin/pip" install -q -r "$REPO/install/requirements.txt" 2>/dev/null \
    || log "  pip could not update deps (offline?) — keeping the installed versions"
fi

log "restarting agent services (if installed)"
for s in support-opencode support-ui support-ui-tls; do
  systemctl is-enabled --quiet "$s" 2>/dev/null && sudo systemctl restart "$s" || true
done
log "done"
