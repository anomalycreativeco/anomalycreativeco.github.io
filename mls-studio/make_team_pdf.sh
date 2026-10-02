#!/bin/zsh
# Build the MLS Studio team-setup PDF in the Anomaly house layout.
#   zsh make_team_pdf.sh            -> "MLS Studio Team Setup.pdf" on the Desktop, values page blank (safe to share)
#   zsh make_team_pdf.sh --values   -> "MLS Studio Team Setup (with values).pdf": values filled from THIS Mac's
#                                      MLS Studio settings and Keychain. Treat that file like a password.
# Needs Google Chrome (printed headless). Brand fonts/logo are read from ~/.claude/brand/anomaly when present.
set -e
HERE="${0:A:h}"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
[[ -x "$CHROME" ]] || { echo "Google Chrome is needed to print the PDF (not found in /Applications)."; exit 1; }
MODE="${1:-}"
OUT="$HOME/Desktop/MLS Studio Team Setup.pdf"
[[ "$MODE" == "--values" ]] && OUT="$HOME/Desktop/MLS Studio Team Setup (with values).pdf"
TMP="$(mktemp -d)"
cp "$HERE/team-setup.html" "$TMP/doc.html"

/usr/bin/python3 - "$TMP/doc.html" "$MODE" "$HERE" <<'EOF'
import base64, json, os, subprocess, sys, datetime, socket
path, mode, here = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(path).read()

# brand assets: the kit on this Mac, else the logo shipped with the app and system fonts
brand = os.path.expanduser("~/.claude/brand/anomaly")
def b64(p):
    try:
        return base64.b64encode(open(p, "rb").read()).decode()
    except Exception:
        return ""
logo = b64(os.path.join(brand, "logo-white.png")) or b64(os.path.join(here, "static", "logo-white.png"))
s = s.replace("__LOGO__", logo)
s = s.replace("__CREATO_REG__", b64(os.path.join(brand, "CreatoDisplay-Regular.otf")))
s = s.replace("__CREATO_MED__", b64(os.path.join(brand, "CreatoDisplay-Medium.otf")))

cfg = {}
try:
    cfg = json.load(open(os.path.expanduser("~/Library/Application Support/Anomaly Studio Hub/mls-studio/config.json")))
except Exception:
    pass
def kc(key):
    r = subprocess.run(["security", "find-generic-password", "-s", "anomaly-studio-hub.mls-studio", "-a", key, "-w"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""
blank = "paste from the private note Daniel shares"
want = mode == "--values"
vals = {
    "AUTOHDR_CLIENT_ID": cfg.get("autohdr_client_id", "") if want else "",
    "AUTOHDR_CLIENT_SECRET": kc("autohdr_client_secret") if want else "",
    "FRAMEIO_CLIENT_ID": cfg.get("frameio_client_id", "") if want else "",
    "FRAMEIO_CLIENT_SECRET": kc("frameio_client_secret") if want else "",
    "SLACK_WEBHOOK_URL": kc("slack_webhook_url") if want else "",
    "HUB_SYNC_KEY": (kc("hub_sync_key") or open(os.path.expanduser("~/Library/Application Support/anomaly-social/mlskey")).read().strip() if os.path.exists(os.path.expanduser("~/Library/Application Support/anomaly-social/mlskey")) else kc("hub_sync_key")) if want else "",
}
classes = {"AH_ID_CLASS": "AUTOHDR_CLIENT_ID", "AH_SECRET_CLASS": "AUTOHDR_CLIENT_SECRET", "FIO_ID_CLASS": "FRAMEIO_CLIENT_ID",
           "FIO_SECRET_CLASS": "FRAMEIO_CLIENT_SECRET", "SLACK_CLASS": "SLACK_WEBHOOK_URL", "HUB_CLASS": "HUB_SYNC_KEY"}
esc = lambda v: v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
for k, v in vals.items():
    s = s.replace("{{%s}}" % k, esc(v) if v else blank)
for c, k in classes.items():
    s = s.replace("{{%s}}" % c, "" if vals[k] else "blank")
s = s.replace("{{GENERATED}}", datetime.date.today().strftime("%B %-d, %Y")).replace("{{HOST}}", socket.gethostname())
open(path, "w").write(s)
missing = [k for k, v in vals.items() if want and not v]
if missing:
    print("Not set on this Mac (left blank):", ", ".join(missing))
EOF

"$CHROME" --headless=new --disable-gpu --no-pdf-header-footer --print-to-pdf="$OUT" "file://$TMP/doc.html" >/dev/null 2>&1
rm -rf "$TMP"
echo "Wrote: $OUT"
[[ "$MODE" == "--values" ]] && echo "This file contains the studio's API secrets. Share it privately (1Password, direct message), never in a channel."
exit 0
