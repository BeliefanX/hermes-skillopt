#!/usr/bin/env bash
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_DIR="${HERMES_HOME:-$HOME/.hermes}/plugins"
mkdir -p "$PLUGIN_DIR"
TARGET="$PLUGIN_DIR/hermes-skillopt"
if [ -L "$TARGET" ] || [ ! -e "$TARGET" ]; then
  ln -sfn "$REPO_DIR" "$TARGET"
  echo "Installed symlink: $TARGET -> $REPO_DIR"
else
  echo "Refusing to overwrite existing non-symlink: $TARGET" >&2
  exit 1
fi
