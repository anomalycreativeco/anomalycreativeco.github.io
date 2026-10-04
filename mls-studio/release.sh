#!/bin/zsh
# MLS Studio release check. Run before every commit that changes the app:
#   zsh mls-studio/release.sh "One line on what changed, written for the editors"
# It refuses to go on unless the two version numbers agree, the version was bumped when the code changed, and the
# code parses. Then it writes version.json. Every running copy of MLS Studio, and the hub's MLS Studio page, read
# that file from the hub site to learn a newer version exists, so a release without it reaches nobody.
set -e
cd "${0:A:h}"
NOTE="$1"
[[ -n "$NOTE" ]] || { echo "Give a one-line note: zsh release.sh \"What changed\""; exit 1; }
V=$(sed -n 's/^APP_VERSION = "\([^"]*\)".*/\1/p' server.py)
P=$(sed -n 's/.*const APP_VERSION = "\([^"]*\)".*/\1/p' static/app.js)
[[ -n "$V" && "$V" == "$P" ]] || { echo "Version mismatch: server.py has '$V', static/app.js has '$P'. Bump both together."; exit 1; }
OLD=$(git show HEAD:mls-studio/version.json 2>/dev/null | /usr/bin/python3 -c 'import sys,json; print(json.load(sys.stdin).get("version",""))' 2>/dev/null || true)
if [[ "$OLD" == "$V" ]] && ! git diff --quiet HEAD -- server.py frameio_mcp.py static install.sh; then
  echo "The app changed but the version is still $V. Bump APP_VERSION in server.py and static/app.js."; exit 1
fi
/usr/bin/python3 -c "
for f in ('server.py', 'frameio_mcp.py'):
    compile(open(f, encoding='utf-8').read(), f, 'exec')
"
JSC=/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc
if [[ -x "$JSC" ]]; then
  OUT=$("$JSC" static/app.js 2>&1 || true)
  [[ "$OUT" == *SyntaxError* ]] && { echo "static/app.js does not parse: $OUT"; exit 1; }
fi
FILES=$(sed -n 's/^for f in \(.*\); do$/\1/p' install.sh)
[[ -n "$FILES" ]] || { echo "Could not read the file list from install.sh"; exit 1; }
/usr/bin/python3 - "$V" "$NOTE" ${=FILES} <<'PY'
import json, os, sys, time
v, note, files = sys.argv[1], sys.argv[2], sys.argv[3:]
missing = [f for f in files if not os.path.exists(f)]
if missing:
    sys.exit("install.sh lists files that do not exist: %s" % ", ".join(missing))
json.dump({"version": v, "notes": note, "released": time.strftime("%Y-%m-%d"), "files": files}, open("version.json", "w"), indent=1)
print("version.json -> %s (%d files): %s" % (v, len(files), note))
PY
