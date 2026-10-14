#!/usr/bin/env bash
# bootstrap.sh — turn a freshly installed GB10 box (DGX Spark, ASUS GX10, …: DGX OS, OOBE done, internet) into a
# Workbench field unit. Run as the operator user (default "admin") from a clone of this repo:
#
#   git clone https://github.com/Theclimateguy/Workbench.git ~/Workbench
#   cd ~/Workbench && ./install/bootstrap.sh [--with-deep] [--password PW] [--skip-models]
#
# Idempotent and resumable: every phase leaves a marker in /var/lib/support/bootstrap/ and is
# skipped on re-run; delete a marker to redo that phase. Downloads ~22 GiB (fast) or ~103 GiB
# (with --with-deep: ~83 GiB) plus the ~12 GiB vLLM image, straight onto this machine.
# Total: ~30 min on a 1 Gbit line for fast, ~1.5 h with deep. Model load after that ~6 min.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck disable=SC1091
. "$REPO/install/versions.lock"
MARK=/var/lib/support/bootstrap
RUN_USER=$(id -un)
WITH_DEEP=0; SKIP_MODELS=0; WEB_PW=""
while [ $# -gt 0 ]; do
  case "$1" in
    --with-deep) WITH_DEEP=1 ;;
    --skip-models) SKIP_MODELS=1 ;;
    --password) WEB_PW=$2; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac; shift
done
log()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mWARNING: %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
phase() { # phase NAME -> returns 1 (skip) if done
  if [ -f "$MARK/$1.done" ]; then echo "    [$1] already done, skipping"; return 1; fi; log "$1"; return 0; }
done_() { sudo touch "$MARK/$1.done"; }

[ "$(id -u)" != 0 ] || die "run as the operator user (with sudo), not as root"
sudo -n true 2>/dev/null || die "sudo without a password is required for the operator user (sudoers NOPASSWD, as DGX OOBE sets for its first user)"
sudo install -d -m 755 "$MARK"

# ---------------------------------------------------------------- 0. preflight
if phase preflight; then
  [ "$(uname -m)" = aarch64 ] || die "this is not an arm64 machine"
  [ -f /etc/dgx-release ] || die "/etc/dgx-release missing — install DGX OS first (docs/DEPLOY.md)"
  # shellcheck disable=SC1091
  . /etc/dgx-release
  echo "    DGX OS $DGX_SWBUILD_VERSION, platform ${DGX_PLATFORM:-?}, host $(hostname)"
  if [ "$(printf '%s\n%s\n' "$DGX_OS_MIN" "$DGX_SWBUILD_VERSION" | sort -V | head -1)" != "$DGX_OS_MIN" ]; then
    # /etc/dgx-release is not always bumped by OTA updates (device #1 reports 7.5.0 on kernel 7.0.0-1019 after
    # the 7.6.0 OTA), so this is a warning, not a stop. STRICT_OS=1 makes it fatal.
    warn "DGX OS $DGX_SWBUILD_VERSION is older than the validated $DGX_OS_MIN (kernel $(uname -r), driver $(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null))."
    warn "If this box never got the OTA update: sudo apt update && sudo apt full-upgrade -y && sudo reboot, then re-run bootstrap."
    [ "${STRICT_OS:-0}" = 1 ] && die "aborting (STRICT_OS=1)"; sleep 5
  fi
  command -v nvidia-smi >/dev/null && nvidia-smi -L | sed 's/^/    /' || die "nvidia-smi not found"
  command -v docker >/dev/null || die "docker not found (should ship with DGX OS)"
  need=$(( 40 + (SKIP_MODELS==0 ? 25 : 0) + (WITH_DEEP==1 ? 70 : 0) ))
  free=$(df -BG --output=avail /opt | tail -1 | tr -dc '0-9')
  [ "$free" -gt "$need" ] || die "need ~${need} GiB free under /opt, have ${free} GiB"
  curl -fsS -m 10 https://huggingface.co >/dev/null || die "no internet (huggingface.co unreachable) — bootstrap needs a connection"
  done_ preflight
fi

# ---------------------------------------------------------------- 1. system
if phase system; then
  sudo apt-get update -qq
  # shellcheck disable=SC2046
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $(grep -vE '^\s*(#|$)' "$REPO/install/apt-packages.txt") >/dev/null
  sudo usermod -aG docker "$RUN_USER"
  # DGX Spark FE ships docker without the nvidia runtime registered (no /etc/docker/daemon.json); ASUS OEM has it.
  if ! docker info 2>/dev/null | grep -qE "Runtimes:.*nvidia"; then
    sudo nvidia-ctk runtime configure --runtime=docker >/dev/null 2>&1 && sudo systemctl restart docker && echo "    nvidia runtime registered in docker"
  fi
  # A field box must not fall asleep while the agent works.
  sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1 || true
  if [ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
    gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing' 2>/dev/null || true
    gsettings set org.gnome.desktop.session idle-delay 0 2>/dev/null || true
  fi
  # OOBE hotspot/dashboard are not needed; telemetry stays off.
  sudo systemctl disable --now dgx-oobe-hotspot dgx-oobe-hotspot-watchdog >/dev/null 2>&1 || true
  echo "$REPO" | sudo tee /etc/support/workbench.path >/dev/null 2>&1 || { sudo install -d /etc/support; echo "$REPO" | sudo tee /etc/support/workbench.path >/dev/null; }
  done_ system
fi

# ---------------------------------------------------------------- 2. python venv
if phase venv; then
  sudo install -d -o "$RUN_USER" -g "$RUN_USER" /opt/support
  [ -x /opt/support/venv/bin/python ] || python3 -m venv /opt/support/venv
  /opt/support/venv/bin/pip install -q --upgrade pip
  /opt/support/venv/bin/pip install -q -r "$REPO/install/requirements.txt"
  /opt/support/venv/bin/python -c "import mcp, fastapi, uvicorn, httpx, huggingface_hub; print('    python deps ok')"
  done_ venv
fi

# ---------------------------------------------------------------- 3. opencode
if phase opencode; then
  if [ "$("$HOME/.opencode/bin/opencode" --version 2>/dev/null || true)" != "$OPENCODE_VERSION" ]; then
    curl -fsSL https://opencode.ai/install | bash -s -- --version "$OPENCODE_VERSION" >/dev/null
  fi
  "$HOME/.opencode/bin/opencode" --version | sed 's/^/    opencode /'
  done_ opencode
fi

# ---------------------------------------------------------------- 4. software
if phase deploy; then
  RUN_USER=$RUN_USER "$REPO/install/deploy.sh"
  done_ deploy
fi

# ---------------------------------------------------------------- 5. vLLM image
if phase vllm-image; then
  sudo docker pull -q "vllm/vllm-openai@$VLLM_IMAGE_DIGEST" >/dev/null
  sudo docker tag "vllm/vllm-openai@$VLLM_IMAGE_DIGEST" "$VLLM_IMAGE"
  echo "    $VLLM_IMAGE ($(sudo docker image inspect "$VLLM_IMAGE" --format '{{.Size}}' | awk '{printf "%.1f GiB", $1/1073741824}'))"
  # GPU visible inside containers?
  sudo docker run --rm --gpus all --entrypoint nvidia-smi "$VLLM_IMAGE" -L 2>/dev/null | sed "s/^/    container sees: /" || die "GPU not visible in container — check nvidia-container-toolkit"
  done_ vllm-image
fi

# ---------------------------------------------------------------- 5b. workbench sandbox image
if phase workbench-image; then
  support-ctl workbench build || warn "workbench image not built (no internet?) — run 'support-ctl workbench build' later"
  done_ workbench-image
fi

# ---------------------------------------------------------------- 6. models
if phase ui-assets; then "$REPO/install/fetch-ui-assets.sh"; done_ ui-assets; fi
if phase library-model; then "$REPO/install/fetch-library-model.sh"; done_ library-model; fi
if [ "$SKIP_MODELS" = 0 ]; then
  if phase model-fast; then "$REPO/install/fetch-models.sh" fast; done_ model-fast; fi
  if [ "$WITH_DEEP" = 1 ] && phase model-deep; then "$REPO/install/fetch-models.sh" deep; done_ model-deep; fi
fi

# ---------------------------------------------------------------- 7. services
if phase services; then
  support-ctl status >/dev/null 2>&1 || true          # generates nothing yet, but validates the CLI
  [ -f /opt/support/keys/agent_ed25519 ] || { mkdir -p /opt/support/keys; ssh-keygen -q -t ed25519 -N "" -C "support-agent@$(hostname)" -f /opt/support/keys/agent_ed25519; }
  sudo systemctl enable llm@fast >/dev/null
  if [ -n "$WEB_PW" ]; then support-ctl web "$WEB_PW"; else support-ctl web; fi
  if [ "$SKIP_MODELS" = 0 ]; then
    echo "    starting llm@fast (first model load ~6 min) ..."
    support-ctl model fast
  fi
  done_ services
fi

# ---------------------------------------------------------------- 8. operator conveniences
if phase desktop; then
  support-ctl install-desktop || true
  [ -f "$REPO/docs/manual/operator-manual.pdf" ] && { mkdir -p "$HOME/Desktop"; cp "$REPO/docs/manual/operator-manual.pdf" "$HOME/Desktop/"; }
  support-ctl net hotspot auto on || warn "hotspot not configured (no Wi-Fi adapter?)"
  done_ desktop
fi

# ---------------------------------------------------------------- 9. verify
if phase verify; then
  if [ "$SKIP_MODELS" = 0 ]; then
    /opt/support/venv/bin/python "$REPO/tests/bench.py" --model fast --concurrency 2 | sed 's/^/    /'
  fi
  # Register this machine as target "self" so the MCP smoke test can run without a workstation.
  if [ ! -f /opt/support/targets/self.env ]; then
    sudo bash /opt/support/bin/onboard-target.sh "$(cat /opt/support/keys/agent_ed25519.pub)" >/dev/null
    printf 'HOST=127.0.0.1\nUSER=support-agent\nPORT=22\nKEY=/opt/support/keys/agent_ed25519\n' > /opt/support/targets/self.env
  fi
  /opt/support/venv/bin/python "$REPO/tests/mcp_smoke.py" --target self | tail -3 | sed 's/^/    /'
  /opt/support/venv/bin/python "$REPO/tests/connectors_smoke.py" | tail -3 | sed 's/^/    /' || warn "connector smoke test failed (see above)"
  done_ verify
fi

log "Workbench is ready"
support-ctl status
cat <<MSG

  Web UI password is in /etc/support/web.env (login: support). Desktop launcher: "Spark Support".
  Next: register the field machine   support-ctl target add ws USER@HOST
        drop its docs into           /opt/support/workspace/docs/   then  support-ctl docs reload
        log out and back in once so the docker group applies to your shell.
MSG
