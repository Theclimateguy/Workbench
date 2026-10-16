#!/usr/bin/env bash
# install.sh — install or update the research chat on this GB10 box (idempotent).
#   research/ -> /opt/research (code + venv), chats and uploads in /var/lib/research,
#   settings in /etc/research/research.env (created once: bind address, password, tokens).
#   Machine tools (shell, files, downloads) run as the unprivileged user `researcher` in /var/lib/research/workspace,
#   behind an outbound network fence (bin/runner-net: internet, DNS and the LLM API only).
# Needs the LLM stack (llm@fast or llm@deep on 127.0.0.1:8000) and docker for SearXNG.
set -euo pipefail
SRC=$(cd "$(dirname "$0")" && pwd)
DEST=/opt/research
RUN_USER=${RUN_USER:-$(id -un)}
RUNNER=researcher
DATA=/var/lib/research
log() { printf '\033[1;34m[research]\033[0m %s\n' "$*"; }
[ "$(id -u)" != 0 ] || { echo "run as the operator user (with sudo rights), not root" >&2; exit 1; }

sudo install -d -o "$RUN_USER" -g "$RUN_USER" "$DEST" /var/lib/research
sudo install -d -m 755 /etc/research
log "code -> $DEST"
rsync -a --delete --exclude venv/ --exclude data/ --exclude __pycache__/ "$SRC/" "$DEST/"

if ! sudo test -s /etc/research/research.env; then
  log "first install: /etc/research/research.env (password for non-local browsers is printed once)"
  PW=$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-16)
  sudo tee /etc/research/research.env >/dev/null <<EOF
# Research chat settings. Restart after editing: sudo systemctl restart research-chat
RESEARCH_BIND=0.0.0.0
RESEARCH_PORT=8090
# Basic auth (user: research) for any client that is not this machine; empty = no password
RESEARCH_PASSWORD=$PW
LLM_URL=http://127.0.0.1:8000
SEARXNG_URL=http://127.0.0.1:8888
SEARXNG_SECRET=$(openssl rand -hex 32)
# Optional: GitHub fine-grained read-only token (code search, private repos, higher rate limits)
GITHUB_TOKEN=
# Optional: Zenodo personal token
ZENODO_TOKEN=
# Optional: OpenAlex key (free, openalex.org/settings/api; without one a small daily budget is shared per IP)
OPENALEX_API_KEY=
# Optional: contact e-mail sent to OpenAlex/Crossref (polite pool)
OPENALEX_MAILTO=
EOF
  sudo chmod 600 /etc/research/research.env
  echo "    web password (user 'research'): $PW"
fi

log "python venv"
[ -x "$DEST/venv/bin/python" ] || python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install -q --upgrade pip
"$DEST/venv/bin/pip" install -q -r "$DEST/requirements.txt"

log "machine tools: user $RUNNER (no sudo, no docker), workspace $DATA/workspace"
if ! id "$RUNNER" >/dev/null 2>&1; then
  sudo useradd --system --user-group --home-dir "$DATA/runner" --no-create-home --shell /bin/bash \
    --comment "Research chat machine tools" "$RUNNER"
fi
id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx "$RUNNER" || sudo usermod -aG "$RUNNER" "$RUN_USER"  # chat serves results
sudo install -d -o "$RUNNER" -g "$RUNNER" -m 700 "$DATA/runner"
sudo install -d -o "$RUNNER" -g "$RUNNER" -m 2775 "$DATA/workspace"
# /ws: the same path in commands and in chat links (/ws/<file> is served by the chat)
[ -e /ws ] && [ ! -L /ws ] || sudo ln -sfn "$DATA/workspace" /ws
# chats and uploads: operator only (the runner may traverse $DATA to reach its own dirs, nothing else)
sudo chmod 711 "$DATA"
sudo chmod 700 "$DATA/uploads" 2>/dev/null || true
sudo find "$DATA" -maxdepth 1 -name 'chats.db*' -exec chmod 600 {} +
printf '%s ALL=(%s) NOPASSWD: %s %s\n' "$RUN_USER" "$RUNNER" "$DEST/venv/bin/python" "$DEST/tools/machine.py" \
  | sudo tee /etc/sudoers.d/92-research-runner >/dev/null
sudo chmod 440 /etc/sudoers.d/92-research-runner
sudo visudo -cf /etc/sudoers.d/92-research-runner >/dev/null || { sudo rm -f /etc/sudoers.d/92-research-runner; log "  runner sudoers rule rejected, removed"; }
sudo install -m 644 "$DEST/systemd/research-runner-net.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --quiet research-runner-net
sudo systemctl restart research-runner-net
log "runner python (scientific stack, a few minutes the first time)"
sudo -u "$RUNNER" -H bash -c "cd '$DATA/runner' && { [ -x venv/bin/python ] || python3 -m venv venv; } \
  && venv/bin/pip install -q --upgrade pip && venv/bin/pip install -q -r '$DEST/runner-requirements.txt'"

log "systemd units (user: $RUN_USER)"
for u in research-chat.service research-search.service research-mdns.service; do
  sed -e "s|^User=admin|User=$RUN_USER|; s|^Group=admin|Group=$RUN_USER|" "$DEST/systemd/$u" \
    | sudo tee "/etc/systemd/system/$u" >/dev/null
done
sudo systemctl daemon-reload

log "sudo rule: the chat may load/unload the LLM profiles (llm@fast|deep|nemotron)"
rule=""
for p in fast deep nemotron; do for a in start stop; do rule+="/usr/bin/systemctl $a llm@$p, "; done; done
echo "$RUN_USER ALL=(root) NOPASSWD: ${rule%, }" | sudo tee /etc/sudoers.d/91-research >/dev/null
sudo chmod 440 /etc/sudoers.d/91-research
sudo visudo -cf /etc/sudoers.d/91-research >/dev/null || { sudo rm -f /etc/sudoers.d/91-research; log "  sudoers rule rejected, removed"; }

log "desktop launcher + app menu entry"
install -Dm644 "$DEST/static/icons/icon-512.png" "$HOME/.local/share/icons/hicolor/512x512/apps/research-chat.png"
install -Dm755 "$DEST/desktop/research.desktop" "$HOME/.local/share/applications/research.desktop"
mkdir -p "$HOME/Desktop" && install -m755 "$DEST/desktop/research.desktop" "$HOME/Desktop/research.desktop"
gtk-update-icon-cache -q -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus} \
  gio set "$HOME/Desktop/research.desktop" metadata::trusted true 2>/dev/null || true

sudo systemctl enable --quiet research-search research-chat
sudo systemctl restart research-search research-chat
if command -v avahi-publish >/dev/null; then sudo systemctl enable --quiet --now research-mdns; sudo systemctl restart research-mdns; fi

for _ in $(seq 30); do curl -sf http://127.0.0.1:8888/healthz >/dev/null && break; sleep 2; done
curl -sf "http://127.0.0.1:8888/search?q=test&format=json" >/dev/null && log "SearXNG ok" || log "SearXNG not answering yet: docker logs research-searxng"
sleep 3
PORT=$(sudo sed -n 's/^RESEARCH_PORT=//p' /etc/research/research.env)
curl -sf "http://127.0.0.1:$PORT/api/status" && echo && log "open http://research.local:$PORT (or http://$(hostname -I | awk '{print $1}'):$PORT)"
