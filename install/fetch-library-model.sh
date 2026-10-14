#!/usr/bin/env bash
# fetch-library-model.sh — download the CPU embedding model for the offline library's semantic search
# (versions.lock LIBRARY_EMBED_MODEL, ~0.25 GiB, fastembed ONNX) into /opt/library/models, then embed every chunk
# that has no vector yet. Needs internet once; without it the library still works with keyword search (BM25).
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck disable=SC1091
. "$REPO/install/versions.lock"
PY=${PY:-/opt/support/venv/bin/python}
export LIBRARY_DIR=${LIBRARY_DIR:-/opt/library} LIBRARY_EMBED_MODEL="$LIBRARY_EMBED_MODEL"
mkdir -p "$LIBRARY_DIR/models"
"$PY" -c "import fastembed" 2>/dev/null || "${PY%/python}/pip" install -q "fastembed>=0.7,<1"
echo "  library embedding model: $LIBRARY_EMBED_MODEL"
"$PY" - <<PY
import os
from fastembed import TextEmbedding
TextEmbedding(os.environ["LIBRARY_EMBED_MODEL"], cache_dir=os.path.join(os.environ["LIBRARY_DIR"], "models"))
print("    model cached in", os.environ["LIBRARY_DIR"] + "/models")
PY
HF_HUB_OFFLINE=1 "$PY" "$REPO/agent/mcp/library_core.py" embed | sed 's/^/    /'
