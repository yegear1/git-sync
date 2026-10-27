#!/usr/bin/env bash
# ==============================================================================
# git-sync — Global Installer Script
# Idempotently installs git-sync executable symlink into ~/.local/bin/git-sync
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LAUNCHER="$REPO_ROOT/git-sync"

echo "📦 git-sync global installer"
echo "Repository root: $REPO_ROOT"

if [ ! -f "$LAUNCHER" ]; then
    echo "❌ Error: Launcher script not found at $LAUNCHER" >&2
    exit 1
fi

chmod +x "$LAUNCHER"

# Delegate to python3 CLI installer
if command -v python3 >/dev/null 2>&1; then
    PYTHONPATH="$REPO_ROOT/src" python3 "$LAUNCHER" install "$@"
    exit $?
fi

# Fallback symlinking logic
TARGET_DIR="${1:-$HOME/.local/bin}"
mkdir -p "$TARGET_DIR"
DEST="$TARGET_DIR/git-sync"

if [ -L "$DEST" ] && [ "$(readlink -f "$DEST")" = "$(readlink -f "$LAUNCHER")" ]; then
    echo "✅ git-sync is already installed at $DEST"
else
    ln -sf "$LAUNCHER" "$DEST"
    echo "🚀 Successfully installed git-sync to $DEST -> $LAUNCHER"
fi

if [[ ":$PATH:" != *":$TARGET_DIR:"* ]]; then
    echo ""
    echo "⚠️  NOTE: $TARGET_DIR is not in your \$PATH."
    echo "   Add the following line to your ~/.bashrc or ~/.zshrc:"
    echo "   export PATH=\"$TARGET_DIR:\$PATH\""
    echo ""
fi
