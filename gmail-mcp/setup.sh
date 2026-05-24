#!/usr/bin/env bash
# One-time setup: create venv and install dependencies

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 -m venv "$SCRIPT_DIR/.venv"
"$SCRIPT_DIR/.venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

# Create config directory for the service account key
mkdir -p "$HOME/.config/claude-gmail"

echo ""
echo "Setup complete."
echo ""
echo "Next steps:"
echo "  1. Place your service account JSON key at:"
echo "     ~/.config/claude-gmail/service-account.json"
echo ""
echo "  2. Add this MCP server to ~/.claude/settings.json:"
echo ""
cat <<EOF
  "mcpServers": {
    "gmail": {
      "command": "$SCRIPT_DIR/.venv/bin/python",
      "args": ["$SCRIPT_DIR/server.py"],
      "env": {
        "GMAIL_DELEGATED_USER": "clark@willcrestpartners.com"
      }
    }
  }
EOF
