#!/usr/bin/env bash
# fetch-models.sh [fast|deep|nemotron|all] — download model weights into /opt/models at the revisions
# pinned in versions.lock. Resumable (re-run after a dropped connection). ~22 GiB fast, ~61 GiB deep, ~81 GiB nemotron.
# Optional: HF_TOKEN env for higher rate limits; HF_ENDPOINT for a mirror.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
MODELS_DIR=${MODELS_DIR:-/opt/models}
HF=${HF:-/opt/support/venv/bin/hf}
LOG=${FETCH_LOG:-/opt/build/logs/fetch-models.log}
mkdir -p "$(dirname "$LOG")" 2>/dev/null || LOG=$HOME/fetch-models.log   # no sudo yet (pre-bootstrap staging): log in $HOME
# shellcheck disable=SC1091
. "$REPO/install/versions.lock"
want=${1:-fast}

fetch() {  # fetch "repo|rev|dir|GiB"
  IFS='|' read -r repo rev dir gib <<<"$1"
  local dest="$MODELS_DIR/$dir"
  if [ -f "$dest/.workbench-rev" ] && [ "$(cat "$dest/.workbench-rev")" = "$rev" ]; then
    echo "  $dir: already at $rev"; return
  fi
  local free; free=$(df -BG --output=avail "$MODELS_DIR" | tail -1 | tr -dc '0-9')
  [ "$free" -gt $((gib + 10)) ] || { echo "not enough free space for $dir (${gib} GiB needed, ${free} GiB free)" >&2; exit 1; }
  echo "  $dir: downloading $repo@$rev (~${gib} GiB) ..."
  mkdir -p "$dest"
  HF_HUB_ENABLE_HF_TRANSFER=0 "$HF" download "$repo" --revision "$rev" --local-dir "$dest" \
    --exclude "*.gguf" --exclude "original/*" --exclude "metal/*" 2>&1 | tee -a "$LOG" | grep -vE "^\s*$" | tail -2
  rm -rf "$dest/.cache"
  echo "$rev" > "$dest/.workbench-rev"
  echo "  $dir: done ($(du -sh "$dest" | cut -f1))"
}

fetch_tiktoken() {  # gpt-oss (Harmony) needs the tiktoken vocab files at runtime; vLLM in the field has no internet.
  IFS='|' read -r _ _ dir _ <<<"$MODEL_DEEP"
  local d="$MODELS_DIR/$dir/tiktoken"; mkdir -p "$d"
  for n in o200k_base cl100k_base; do
    local u="https://openaipublic.blob.core.windows.net/encodings/$n.tiktoken"
    local h; h=$(printf '%s' "$u" | sha1sum | cut -d' ' -f1)       # tiktoken-rs cache key = sha1(url)
    [ -s "$d/$h" ] || curl -fsSL -o "$d/$h" "$u"
    echo "  tiktoken $n -> $d/$h"
  done
}

case "$want" in
  fast) fetch "$MODEL_FAST" ;;
  deep) fetch "$MODEL_DEEP"; fetch_tiktoken; [ -n "${MODEL_DEEP_DRAFT:-}" ] && fetch "$MODEL_DEEP_DRAFT" ;;
  nemotron) fetch "$MODEL_NEMOTRON"; fetch "$MODEL_NEMOTRON_DRAFT" ;;
  all)  fetch "$MODEL_FAST"; fetch "$MODEL_DEEP"; fetch_tiktoken; [ -n "${MODEL_DEEP_DRAFT:-}" ] && fetch "$MODEL_DEEP_DRAFT" ;;
  *) echo "usage: $0 fast|deep|nemotron|all" >&2; exit 1 ;;
esac
