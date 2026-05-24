#!/usr/bin/env bash
# One-time setup: create venv, install dependencies, run OAuth2 authorization

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 -m venv "$SCRIPT_DIR/.venv"
"$SCRIPT_DIR/.venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

mkdir -p "$HOME/.config/claude-gmail"

echo ""
echo "Setup complete. Dependencies installed."
echo ""
echo "Next steps:"
echo ""
echo "  1. Place your OAuth2 client_secrets.json at:"
echo "     ~/.config/claude-gmail/client_secrets.json"
echo "     (Download from Google Cloud Console → APIs & Services → Credentials)"
echo ""
echo "  2. Run the one-time authorization to sign in as clark@willcrestpartners.com:"
echo "     $SCRIPT_DIR/.venv/bin/python $SCRIPT_DIR/authorize.py"
echo "     (Opens a browser — sign in as clark@willcrestpartners.com ONLY)"
echo "     (Token is saved to ~/.config/claude-gmail/token.json)"
echo ""
echo "  3. Add this block to ~/.claude/settings.json under mcpServers:"
echo ""
cat <<EOF
  "gmail": {
    "command": "$SCRIPT_DIR/.venv/bin/python",
    "args": ["$SCRIPT_DIR/server.py"]
  }
EOF
echo ""
echo "  4. Restart Claude Code. You can now use email tools in any session."
