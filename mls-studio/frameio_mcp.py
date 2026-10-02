#!/usr/bin/env python3
"""
Frame.io V4 MCP server for Claude — stdio transport, no dependencies.

Reuses the Frame.io sign-in that MLS Studio stores in the macOS Keychain, so one Adobe
credential serves both the hub page and Claude sessions. Register with:

    claude mcp add --scope user frameio -- python3 /path/to/mls-studio/frameio_mcp.py

Only JSON-RPC goes to stdout; everything else goes to stderr.
"""
import json
import mimetypes
import os
import shutil
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server as hub  # noqa: E402

PROTOCOL = "2025-06-18"


def elog(msg):
    sys.stderr.write("frameio-mcp: %s\n" % msg)
    sys.stderr.flush()


def client():
    return hub.FrameIO(hub.get_config())


def slim(obj, keys):
    return {k: obj.get(k) for k in keys if k in obj}


# ------------------------------------------------------------------ tools --
def t_whoami(_):
    c = client()
    me = c.me()
    accounts = [slim(a, ("id", "display_name", "name")) for a in c.accounts()]
    return {"user": slim(me, ("id", "name", "email")), "accounts": accounts}


def t_list_accounts(_):
    return [slim(a, ("id", "display_name", "name", "created_at")) for a in client().accounts()]


def t_list_workspaces(a):
    return [slim(w, ("id", "name")) for w in client().workspaces(a["account_id"])]


def t_list_projects(a):
    return [slim(p, ("id", "name", "root_folder_id", "view_url", "status")) for p in client().projects(a["account_id"], a["workspace_id"])]


def t_list_folder(a):
    kids = client().children(a["account_id"], a["folder_id"])
    return [slim(k, ("id", "name", "type", "file_size", "media_type", "status", "view_url", "created_at", "updated_at")) for k in kids]


def t_find_path(a):
    """Walk a '/'-separated path of folder names from a project's root folder."""
    c = client()
    projects = c.projects(a["account_id"], a["workspace_id"])
    proj = next((p for p in projects if p.get("name") == a["project_name"]), None)
    if not proj:
        return {"error": "No project named %r. Projects: %s" % (a["project_name"], [p.get("name") for p in projects])}
    folder_id, trail = proj["root_folder_id"], [proj["name"]]
    for name in [s for s in (a.get("path") or "").split("/") if s.strip()]:
        kids = c.children(a["account_id"], folder_id)
        nxt = next((k for k in kids if k.get("type") == "folder" and k.get("name") == name), None)
        if not nxt:
            return {"error": "No folder %r under %s. Folders here: %s" % (name, " / ".join(trail), [k.get("name") for k in kids if k.get("type") == "folder"])}
        folder_id, trail = nxt["id"], trail + [name]
    return {"folder_id": folder_id, "path": " / ".join(trail), "project_id": proj["id"]}


def t_create_folder(a):
    f, created = client().find_or_create_folder(a["account_id"], a["parent_folder_id"], a["name"])
    return {"created": created, **slim(f, ("id", "name", "parent_id", "view_url"))}


def t_upload_file(a):
    p = Path(a["path"]).expanduser()
    if not p.is_file():
        return {"error": "Not a file: %s" % p}
    f, state = client().upload_file(a["account_id"], a["folder_id"], p, existing_names=None)
    return {"state": state, **slim(f or {}, ("id", "name", "file_size", "status", "view_url"))}


def t_upload_folder(a):
    c = client()
    src = Path(a["path"]).expanduser()
    if not src.is_dir():
        return {"error": "Not a folder: %s" % src}
    existing = {k.get("name") for k in c.children(a["account_id"], a["folder_id"]) if k.get("type") == "file"}
    out = []
    for p in sorted(x for x in src.iterdir() if x.is_file() and not x.name.startswith(".")):
        f, state = c.upload_file(a["account_id"], a["folder_id"], p, existing)
        out.append({"name": p.name, "state": state})
    return {"uploaded": sum(1 for o in out if o["state"] == "uploaded"), "skipped": sum(1 for o in out if o["state"] == "skipped"), "files": out}


def t_get_file(a):
    c = client()
    f = c.unwrap(c.call("GET", "/accounts/%s/files/%s?include=media_links.original,media_links.thumbnail" % (a["account_id"], a["file_id"])))
    links = f.get("media_links") or {}
    return {**slim(f, ("id", "name", "file_size", "media_type", "status", "view_url", "parent_id", "project_id")),
            "download_url": (links.get("original") or {}).get("download_url"), "thumbnail_url": (links.get("thumbnail") or {}).get("download_url")}


def t_download_file(a):
    info = t_get_file(a)
    if not info.get("download_url"):
        return {"error": "No original download link for this file (it may still be processing).", "file": info}
    dest_dir = Path(a.get("dest_dir") or (Path.home() / "Downloads")).expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / info["name"]
    with urllib.request.urlopen(info["download_url"], timeout=600) as r, open(dest, "wb") as fh:
        shutil.copyfileobj(r, fh)
    return {"saved_to": str(dest), "bytes": dest.stat().st_size}


def t_list_comments(a):
    c = client()
    items = c.list_all("/accounts/%s/files/%s/comments?page_size=100" % (a["account_id"], a["file_id"]))
    return [{**slim(x, ("id", "text", "timestamp", "created_at", "updated_at", "page")),
             "author": ((x.get("owner") or x.get("creator") or {}).get("name"))} for x in items]


def t_create_comment(a):
    c = client()
    body = {"data": {"text": a["text"]}}
    if a.get("timestamp") is not None:
        body["data"]["timestamp"] = a["timestamp"]
    r = c.unwrap(c.call("POST", "/accounts/%s/files/%s/comments" % (a["account_id"], a["file_id"]), body))
    return slim(r, ("id", "text", "created_at"))


TOOLS = [
    ("frameio_whoami", "Who is signed in to Frame.io and which accounts are visible. Call this first to get account_id.", {}, t_whoami),
    ("frameio_list_accounts", "List Frame.io accounts available to the signed-in user.", {}, t_list_accounts),
    ("frameio_list_workspaces", "List workspaces in an account.", {"account_id": "string"}, t_list_workspaces),
    ("frameio_list_projects", "List projects in a workspace, with each project's root_folder_id.", {"account_id": "string", "workspace_id": "string"}, t_list_projects),
    ("frameio_list_folder", "List the files and folders inside a folder (use a project's root_folder_id for the top level).", {"account_id": "string", "folder_id": "string"}, t_list_folder),
    ("frameio_find_path", "Resolve a project name plus a '/'-separated folder path (e.g. 'Delivered/214 Hawthorne/MLS') to a folder_id.", {"account_id": "string", "workspace_id": "string", "project_name": "string", "path": "string"}, t_find_path),
    ("frameio_create_folder", "Create a folder inside a folder (returns the existing one if the name already exists).", {"account_id": "string", "parent_folder_id": "string", "name": "string"}, t_create_folder),
    ("frameio_upload_file", "Upload one local file into a folder.", {"account_id": "string", "folder_id": "string", "path": "string"}, t_upload_file),
    ("frameio_upload_folder", "Upload every file in a local folder into a Frame.io folder, skipping names already there.", {"account_id": "string", "folder_id": "string", "path": "string"}, t_upload_folder),
    ("frameio_get_file", "File details plus original download and thumbnail links.", {"account_id": "string", "file_id": "string"}, t_get_file),
    ("frameio_download_file", "Download a file's original to a local folder (default ~/Downloads).", {"account_id": "string", "file_id": "string", "dest_dir": "string?"}, t_download_file),
    ("frameio_list_comments", "Read the comments on a file (where briefs and captions usually live).", {"account_id": "string", "file_id": "string"}, t_list_comments),
    ("frameio_create_comment", "Post a comment on a file. Optional timestamp (seconds) for video.", {"account_id": "string", "file_id": "string", "text": "string", "timestamp": "number?"}, t_create_comment),
]


def schema(params):
    props, req = {}, []
    for k, t in params.items():
        optional = t.endswith("?")
        props[k] = {"type": t.rstrip("?")}
        if not optional:
            req.append(k)
    return {"type": "object", "properties": props, "required": req}


def tool_list():
    return [{"name": n, "description": d, "inputSchema": schema(p)} for n, d, p, _ in TOOLS]


def tool_call(name, args):
    for n, _, _, fn in TOOLS:
        if n == name:
            try:
                res = fn(args or {})
                is_err = isinstance(res, dict) and "error" in res
                return {"content": [{"type": "text", "text": json.dumps(res, indent=1)}], "isError": is_err}
            except hub.ApiError as e:
                return {"content": [{"type": "text", "text": str(e)}], "isError": True}
            except Exception as e:  # noqa
                return {"content": [{"type": "text", "text": "%s: %s" % (type(e).__name__, e)}], "isError": True}
    return {"content": [{"type": "text", "text": "Unknown tool %s" % name}], "isError": True}


# ------------------------------------------------------------- JSON-RPC --
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def handle(msg):
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
              "capabilities": {"tools": {}}, "serverInfo": {"name": "frameio", "version": "1.0.0"},
              "instructions": "Frame.io V4 for the Anomaly studio. Call frameio_whoami first for account_id. Uploads skip files whose names already exist in the target folder."}})
    elif method == "notifications/initialized" or method.startswith("notifications/"):
        return
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": tool_list()}})
    elif method == "tools/call":
        send({"jsonrpc": "2.0", "id": mid, "result": tool_call(params.get("name"), params.get("arguments"))})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found: %s" % method}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            elog("bad json: %r" % line[:120])
            continue
        try:
            handle(msg)
        except Exception as e:  # noqa
            elog("handler error: %s" % e)
            if msg.get("id") is not None:
                send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)}})


if __name__ == "__main__":
    main()
