#!/usr/bin/env bash
# fetch-ui-assets.sh — download browser-side assets for the web UI (offline voice input, PWA):
#   Transformers.js bundle + onnxruntime-web WASM  -> /opt/support/ui-assets/vendor/
#   Whisper ONNX model (versions.lock STT_MODEL)    -> /opt/support/ui-assets/models/<name>/
# ~0.3 GiB. Needs internet once; served by support-ui afterwards, nothing is fetched from CDNs at runtime.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck disable=SC1091
. "$REPO/install/versions.lock"
ASSETS=${UI_ASSETS_DIR:-/opt/support/ui-assets}
HF=${HF:-/opt/support/venv/bin/hf}
mkdir -p "$ASSETS/vendor" "$ASSETS/models"

if [ ! -f "$ASSETS/vendor/.version" ] || [ "$(cat "$ASSETS/vendor/.version")" != "$TRANSFORMERS_JS_VERSION/$ORT_WEB_VERSION" ]; then
  echo "  vendor: transformers.js $TRANSFORMERS_JS_VERSION + onnxruntime-web $ORT_WEB_VERSION"
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/tf.tgz" "https://registry.npmjs.org/@huggingface/transformers/-/transformers-$TRANSFORMERS_JS_VERSION.tgz"
  curl -fsSL -o "$tmp/ort.tgz" "https://registry.npmjs.org/onnxruntime-web/-/onnxruntime-web-$ORT_WEB_VERSION.tgz"
  mkdir -p "$tmp/tf" "$tmp/ort"
  tar xzf "$tmp/tf.tgz" -C "$tmp/tf" package/dist/transformers.min.js
  tar xzf "$tmp/ort.tgz" -C "$tmp/ort"            # bsdtar (macOS) has no --wildcards; extract all, copy what we need
  rm -f "$ASSETS"/vendor/*.js "$ASSETS"/vendor/*.mjs "$ASSETS"/vendor/*.wasm
  cp "$tmp"/tf/package/dist/transformers.min.js "$tmp"/ort/package/dist/ort-wasm-simd-threaded*.wasm "$tmp"/ort/package/dist/ort-wasm-simd-threaded*.mjs "$ASSETS/vendor/"
  echo "$TRANSFORMERS_JS_VERSION/$ORT_WEB_VERSION" > "$ASSETS/vendor/.version"
  rm -rf "$tmp"
fi

IFS='|' read -r repo rev name _ <<<"$STT_MODEL"
dest="$ASSETS/models/$name"
if [ ! -f "$dest/.rev" ] || [ "$(cat "$dest/.rev")" != "$rev" ]; then
  echo "  model: $repo@$rev -> $dest"
  mkdir -p "$dest"
  # Small files one by one over plain HTTPS (hf's multi-pattern --include silently drops some of them),
  # ONNX weights via hf (resumable): fp32 encoder + q4 decoder for WebGPU, q8 pair for the WASM fallback.
  for f in config.json generation_config.json preprocessor_config.json tokenizer.json tokenizer_config.json \
           special_tokens_map.json added_tokens.json normalizer.json quantize_config.json merges.txt vocab.json; do
    curl -fsSL -o "$dest/$f" "${HF_ENDPOINT:-https://huggingface.co}/$repo/resolve/$rev/$f" || echo "    (no $f in repo, skipped)"
  done
  "$HF" download "$repo" --revision "$rev" --local-dir "$dest" \
    --include "onnx/encoder_model.onnx" --include "onnx/encoder_model_quantized.onnx" \
    --include "onnx/decoder_model_merged_q4.onnx" --include "onnx/decoder_model_merged_quantized.onnx" >/dev/null
  for f in config.json tokenizer.json preprocessor_config.json onnx/encoder_model.onnx onnx/decoder_model_merged_q4.onnx; do
    [ -s "$dest/$f" ] || { echo "missing $dest/$f after download" >&2; exit 1; }
  done
  rm -rf "$dest/.cache"
  echo "$rev" > "$dest/.rev"
fi
echo "  ui assets ready: $(du -sh "$ASSETS" | cut -f1)"
