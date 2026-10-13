#!/usr/bin/env bash
# stand.sh — acceptance stand for the agent (PLAN.md §5) on a disposable systemd container ("evalbox").
# Runs ON THE WORKBENCH BOX (docker + the deployed stack). Cases live in tests/eval/cases/NN-name/{task.md,inject.sh,check.sh}.
#   stand.sh up            build the image (needs internet once), start evalbox, register it as device "evalbox"
#   stand.sh reset         fresh container (= VM snapshot restore), keeps the image
#   stand.sh list          cases and whether this stand can run them
#   stand.sh inject NN     break the box per case NN
#   stand.sh check NN      exit 0 if the box is fixed
#   stand.sh shell         root shell inside evalbox
#   stand.sh down          stop and remove evalbox (device file is kept)
# Full automated run through the agent: tests/eval/run.py (uses this script).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
NAME=${EVALBOX_NAME:-evalbox}
IMAGE=${EVALBOX_IMAGE:-workbench-evalbox:local}
SSH_PORT=${EVALBOX_SSH_PORT:-2222}
SUPPORT_DIR=${SUPPORT_DIR:-/opt/support}
TARGETS=${SUPPORT_TARGETS_DIR:-$SUPPORT_DIR/targets}
KEY=$SUPPORT_DIR/keys/agent_ed25519
die() { echo "stand: $*" >&2; exit 1; }
case_dir() { ls -d "$HERE"/cases/"$1"-* 2>/dev/null | head -1; }

start_box() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run -d --name "$NAME" --hostname ws-eval --privileged --cgroupns=private \
    --tmpfs /run --tmpfs /run/lock --tmpfs /tmp --tmpfs /var/log:size=256m,mode=755 \
    -v /sys/fs/cgroup:/sys/fs/cgroup:rw -p "127.0.0.1:$SSH_PORT:22" "$IMAGE" >/dev/null
  for _ in $(seq 1 30); do docker exec "$NAME" systemctl is-system-running 2>/dev/null | grep -qE 'running|degraded' && break; sleep 1; done
  docker exec "$NAME" systemctl is-active ssh >/dev/null || die "sshd did not start in $NAME (docker logs $NAME)"
  # let the agent in (same onboarding as a real workstation); avahi install is skipped quickly without apt access
  docker exec -i "$NAME" bash -s -- "$(cat "$KEY.pub")" < "$SUPPORT_DIR/bin/onboard-target.sh" | tail -1
  # the box logs to the tmpfs: rsyslog needs its dir back
  docker exec "$NAME" bash -c 'mkdir -p /var/log/journal /var/log/app; systemctl restart rsyslog 2>/dev/null || true'
  mkdir -p "$TARGETS"
  printf 'HOST=127.0.0.1\nUSER=support-agent\nPORT=%s\nKEY=%s\n' "$SSH_PORT" "$KEY" > "$TARGETS/$NAME.env"
  ssh-keygen -R "[127.0.0.1]:$SSH_PORT" >/dev/null 2>&1 || true
  sed -i "/^$NAME /d" "$HOME/.ssh/known_hosts" 2>/dev/null || true     # HostKeyAlias=evalbox changes with every fresh container
  support-ctl target test "$NAME"
}

case "${1:-}" in
  up)
    docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "building $IMAGE ..."; docker build -q -t "$IMAGE" "$HERE" >/dev/null; }
    start_box ;;
  reset) start_box ;;
  list)
    for d in "$HERE"/cases/*/; do
      n=$(basename "$d"); req=$(sed -n 's/^requires: *//p' "$d/meta" 2>/dev/null)
      printf '  %-28s %s\n' "$n" "${req:+[needs $req — skipped on the container stand]}"
    done ;;
  inject|check)
    d=$(case_dir "${2:?case number}"); [ -n "$d" ] || die "no case $2"
    req=$(sed -n 's/^requires: *//p' "$d/meta" 2>/dev/null); [ -z "$req" ] || die "case $(basename "$d") needs $req"
    docker exec -i "$NAME" bash -s < "$d/$1.sh" ;;
  shell) docker exec -it "$NAME" bash ;;
  down) docker rm -f "$NAME" >/dev/null 2>&1 && echo "removed $NAME" ;;
  *) sed -n '2,11p' "$0"; exit 1 ;;
esac
