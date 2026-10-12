#!/usr/bin/env bash
# Dev-only: run the support UI on the Mac without an OpenCode engine (static page, STT/PWA assets testable).
# UI_ASSETS: dir with vendor/ + models/ (fetched by install/fetch-ui-assets.sh with UI_ASSETS_DIR=...).
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
export SUPPORT_UI_ASSETS=${UI_ASSETS:-$REPO/build/ui-assets}
export SUPPORT_TLS_DIR=/nonexistent OPENCODE_URL=http://127.0.0.1:1 SUPPORT_UI_PASSWORD= SUPPORT_WORKSPACE=/tmp SUPPORT_TARGETS_DIR=/nonexistent SUPPORT_AUDIT_LOG=/nonexistent
cd "$REPO/agent/ui"
exec python3 -m uvicorn support_ui:app --host 127.0.0.1 --port 18090 --log-level warning
