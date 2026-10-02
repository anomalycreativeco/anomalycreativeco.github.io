#!/bin/zsh
# MLS Studio — install or update on this Mac, then start it.
#   curl -fsSL https://anomalycreativeco.github.io/mls-studio/install.sh | zsh
# Puts the app in ~/Applications/MLS Studio (no dependencies beyond macOS's Python 3 and sips),
# registers the Frame.io MCP server for Claude Code if ~/.claude.json exists, and opens the page.
set -e
BASE="https://anomalycreativeco.github.io/mls-studio"
DEST="$HOME/Applications/MLS Studio"
mkdir -p "$DEST/static"
for f in server.py frameio_mcp.py README.md install.sh static/index.html static/app.js static/logomark.png static/logomark-white.png static/logo-white.png; do
  curl -fsSL "$BASE/$f?v=$(date +%s)" -o "$DEST/$f"
done
chmod +x "$DEST/server.py" "$DEST/install.sh"
echo "MLS Studio installed in $DEST"

# Register the Frame.io MCP server for Claude Code sessions (user scope), if Claude Code is set up.
if [[ -f "$HOME/.claude.json" ]]; then
  /usr/bin/python3 - "$DEST" <<'EOF'
import json, os, sys
p = os.path.expanduser("~/.claude.json"); dest = sys.argv[1]
try:
    d = json.load(open(p))
except Exception:
    sys.exit(0)
d.setdefault("mcpServers", {})["frameio"] = {"type": "stdio", "command": "/usr/bin/python3", "args": [os.path.join(dest, "frameio_mcp.py")], "env": {}}
json.dump(d, open(p, "w"), indent=2)
print("Registered the frameio MCP server for Claude Code.")
EOF
fi

# Stop a copy that is already running, then start this one.
pkill -f "MLS Studio/server.py" 2>/dev/null || true
pkill -f "mls-studio/server.py" 2>/dev/null || true
sleep 1
echo "Starting MLS Studio… leave this window open while you work (Ctrl-C stops it)."
exec /usr/bin/python3 "$DEST/server.py" --open
