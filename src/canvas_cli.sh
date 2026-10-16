#!/usr/bin/env bash
set -euo pipefail
exec "${CANVAS_INSTALL_DIR:-$HOME/.local/share/canvas-cli}/.venv/bin/canvas" "$@"
