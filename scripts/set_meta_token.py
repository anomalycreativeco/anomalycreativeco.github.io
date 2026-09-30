#!/usr/bin/env python3
"""
Install the Meta access token for the Instagram Analytics collector.

    1. Copy the token in Meta's page (the Copy button next to it).
    2. Run:  python3 scripts/set_meta_token.py

The token is read straight from the clipboard, checked against Meta, saved to
~/Library/Application Support/anomaly-social/meta.json (readable only by this
Mac user), and the clipboard is cleared. It is never printed, logged, or put
anywhere near the hub repo.

    --add     keep the tokens already saved and add this one (Instagram-Login
              route: one token per client account)
    --show    list what the saved token(s) can read, without changing anything
"""
import argparse, json, os, re, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sync_ig_analytics import Graph, GraphError, META_PATH, STATE_DIR, GRAPH_DEFAULT_HOST, GRAPH_DEFAULT_VERSION, load_graphs  # noqa: E402

TOKEN_RE = re.compile(r"^(EA|IG)[A-Za-z0-9_\-]{60,}$")   # Facebook-Login tokens start EAA…, Instagram-Login IGAA…


def describe(graphs):
    seen = {}
    for g in graphs:
        try:
            seen.update(g.visible_accounts())
        except GraphError as e:
            print(f"Meta rejected a saved token: {e}")
    if seen:
        print(f"Connected Instagram accounts ({len(seen)}):")
        for u in sorted(seen):
            print("   @" + u)
    else:
        print("No Instagram accounts are readable yet.")
    return seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--add", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--file", help=argparse.SUPPRESS)     # tests only
    a = ap.parse_args()
    if a.show:
        gs = load_graphs()
        if not gs:
            print("No token saved yet.")
            return 0
        describe(gs)
        return 0
    if a.file:
        token = open(a.file).read().strip()
    else:
        token = subprocess.run(["/usr/bin/pbpaste"], capture_output=True, text=True).stdout.strip()
    if not TOKEN_RE.match(token):
        print("The clipboard doesn't hold a Meta access token (they are one long string starting with EAA… or IGAA…).\n"
              "Copy the token in Meta's page again, then re-run this. Nothing was changed.")
        return 1
    host = "https://graph.instagram.com" if token.startswith("IG") else GRAPH_DEFAULT_HOST
    g = Graph(host, GRAPH_DEFAULT_VERSION, token)
    try:
        seen = g.visible_accounts()
    except GraphError as e:
        print(f"Meta rejected this token: {e}\nNothing was saved.")
        return 1
    cfg = {"version": GRAPH_DEFAULT_VERSION, "tokens": []}
    if a.add:
        try:
            with open(META_PATH) as fh:
                old = json.load(fh)
            cfg["tokens"] = old.get("tokens") or ([{"host": old.get("host") or GRAPH_DEFAULT_HOST, "token": old["token"]}] if old.get("token") else [])
        except (OSError, ValueError):
            pass
    cfg["tokens"] = [t for t in cfg["tokens"] if t.get("token") != token] + [{"host": host, "token": token}]
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = META_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(cfg, fh)
    os.replace(tmp, META_PATH)
    os.chmod(META_PATH, 0o600)
    if not a.file:
        subprocess.run(["/usr/bin/pbcopy"], input="", text=True)     # don't leave the token on the clipboard
    print(f"Token saved ({len(cfg['tokens'])} on file). Clipboard cleared.")
    if seen:
        print(f"It can read {len(seen)} Instagram account(s):")
        for u in sorted(seen):
            print("   @" + u)
    else:
        print("It is valid but can't read any Instagram account yet — assign the client Pages and Instagram accounts "
              "to the system user in Meta Business settings, then run this again with --show.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
