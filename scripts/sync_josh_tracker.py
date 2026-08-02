#!/usr/bin/env python3
"""Josh Holyfield RAW footage tracker sync.

Walks the "Raw Video" tree in the Josh Holyfield Frame.io project (July 2026
onward), resolves each clip's editing status, and mirrors the snapshot into
Firestore doc hub/joshTracker where josh.html and the hub widget read it.

Status resolution, in order of trust:
  1. The Frame.io "Status" metadata field — the In Progress / Needs Review /
     Approved pill set in the UI. Read in bulk via ?include=metadata on each
     folder listing. The field is only present on files that HAVE a status,
     so its absence is itself the signal.
  2. Folder convention — a clip sitting in a "Done"/"In Review" folder is
     legacy work finished before the pills were adopted. Tagged src="folder"
     so the UI can distinguish it from a real pill.
  3. Otherwise "none" = untouched, the backlog this tracker exists to surface.

Run: python3 sync_josh_tracker.py
"""
import json, re, subprocess, sys, urllib.request, urllib.error

FRAMEIO_AUTH = "/Users/danielpan/Desktop/Claude/Anomaly Creative/.claude/skills/frameio-captions-pipeline/scripts/frameio_auth.py"
ACCOUNT_ID = "03600420-7703-498b-b50c-23d55928bd05"
PROJECT_ID = "3a8a9d3b-7e1e-4339-b0d2-eb13a85bd677"        # Josh Holyfield
RAW_FOLDER_ID = "bd13b802-d06a-4cd2-be1f-78d49e664d48"     # "Raw Video"
API = "https://api.frame.io/v4"
UA = "AnomalyRawTracker/1.0"
FIRST_MONTH = "2026-07"

FIRESTORE = "https://firestore.googleapis.com/v1/projects/anomaly-post-pipeline/databases/(default)/documents"
SYNC_KEY = "d432678276ba549785def00d314f4a34"
AUTOMATION_ID = "josh-raw-tracker"

MONTHS = {m.lower(): i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"])}


def http(url, method="GET", body=None, headers=None, token=None):
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("User-Agent", UA)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode() or "{}")


def api_get(path, token):
    """GET with pagination — follows links.next and concatenates data."""
    out, url = [], API + path
    while url:
        page = http(url, token=token)
        data = page.get("data")
        if isinstance(data, list):
            out.extend(data)
        else:
            return data
        nxt = (page.get("links") or {}).get("next")
        url = ("https://api.frame.io" + nxt) if nxt and nxt.startswith("/") else nxt
    return out


def preflight():
    """hub/automations contract: paused → skip the run; note → surface it."""
    try:
        doc = http(FIRESTORE + "/hub/automations")
        lst = json.loads(doc["fields"]["list"]["stringValue"])
        entry = next((a for a in lst if a.get("id") == AUTOMATION_ID), None)
        if not entry:
            return True
        if entry.get("status") == "paused":
            print("Automation is paused in the hub — skipping run.")
            return False
        if entry.get("note"):
            print("Note from Daniel:", entry["note"])
    except Exception:
        pass  # fetch failed / rules not published → proceed normally
    return True


def get_token():
    r = subprocess.run(["python3", FRAMEIO_AUTH, "token"], capture_output=True, text=True)
    tok = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    if r.returncode != 0 or not tok:
        sys.exit("Could not get a Frame.io token: " + (r.stderr or r.stdout))
    return tok


def norm_status(v):
    s = re.sub(r"[^a-z]", "", str(v).lower())
    if "approv" in s: return "approved"
    if "review" in s: return "needs_review"
    if "progress" in s: return "in_progress"
    return None


def metadata_status(child):
    """The Status pill, from the metadata array included in the folder listing.

    Files with no status set simply have no Status field, which is exactly the
    'nobody has touched this' signal the tracker is built on.
    """
    for f in child.get("metadata") or []:
        if str(f.get("field_definition_name", "")).strip().lower() != "status":
            continue
        v = f.get("value")
        if isinstance(v, list) and v:
            v = v[0].get("display_name") if isinstance(v[0], dict) else v[0]
        elif isinstance(v, dict):
            v = v.get("display_name") or v.get("value")
        return norm_status(v)
    return None


def folder_status(path_names):
    for name in reversed([n.lower() for n in path_names]):
        if name == "done": return "approved"
        if "review" in name: return "needs_review"
        if "progress" in name: return "in_progress"
    return None


DATE_IN_FOLDER = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{2,4})$")
DATE_IN_NAME = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def shoot_date(path_names, filename):
    for name in reversed(path_names):
        m = DATE_IN_FOLDER.match(name.strip())
        if m:
            mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            y = y + 2000 if y < 100 else y
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = DATE_IN_NAME.search(filename)
    if m:
        return m.group(0)
    return None


def walk(folder_id, token, path_names, sink):
    for child in api_get(f"/accounts/{ACCOUNT_ID}/folders/{folder_id}/children?include=metadata", token):
        if child.get("type") == "folder":
            walk(child["id"], token, path_names + [child.get("name", "")], sink)
        elif str(child.get("media_type", "")).startswith("video/"):
            top = path_names[0] if path_names else ""
            bucket = "shortform" if "short" in top.lower() else "longform"
            name = child.get("name", "")
            if bucket == "longform" and top and top.lower() != "youtube":
                name = f"{top} · {name}"
            pill = metadata_status(child)
            status = pill or folder_status(path_names) or "none"
            sink[bucket].append({
                "id": child["id"], "name": name,
                "date": shoot_date(path_names, child.get("name", "")),
                "uploadedAt": child.get("created_at"),
                "status": status,
                "src": "pill" if pill else ("folder" if status != "none" else ""),
                "url": f"https://next.frame.io/project/{PROJECT_ID}/view/{child['id']}",
            })


def month_key(name):
    m = re.match(r"^([A-Za-z]+)\s+(\d{4})$", name.strip())
    if not m or m.group(1).lower() not in MONTHS:
        return None
    return f"{int(m.group(2)):04d}-{MONTHS[m.group(1).lower()]:02d}"


def main():
    if not preflight():
        return
    token = get_token()
    months = []
    for f in api_get(f"/accounts/{ACCOUNT_ID}/folders/{RAW_FOLDER_ID}/children", token):
        if f.get("type") != "folder":
            continue
        key = month_key(f.get("name", ""))
        if not key or key < FIRST_MONTH:
            continue
        sink = {"longform": [], "shortform": []}
        walk(f["id"], token, [], sink)
        for b in sink.values():
            b.sort(key=lambda c: (c["date"] or "", c["name"]))
        months.append({"key": key, "label": f["name"].strip(),
                       "longform": sink["longform"], "shortform": sink["shortform"]})
        print(f"  {f['name']}: {len(sink['longform'])} long form, {len(sink['shortform'])} short form")

    months.sort(key=lambda m: m["key"])
    clips = [c for m in months for c in m["longform"] + m["shortform"]]
    untouched = sum(1 for c in clips if c["status"] == "none")
    pills = sum(1 for c in clips if c.get("src") == "pill")
    print(f"  status pills read: {pills} · folder-inferred: "
          f"{sum(1 for c in clips if c.get('src') == 'folder')} · untouched: {untouched}")
    payload = {"months": months}

    if "--out" in sys.argv:
        path = sys.argv[sys.argv.index("--out") + 1]
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=2)
        print(f"Wrote snapshot to {path} ({len(clips)} clips, {untouched} untouched).")
        return

    import time
    body = {"fields": {
        "syncKey": {"stringValue": SYNC_KEY},
        "at": {"integerValue": str(int(time.time() * 1000))},
        "data": {"stringValue": json.dumps(payload)},
    }}
    try:
        http(FIRESTORE + "/hub/joshTracker", method="PATCH", body=body)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            print("Firestore write refused (403) — the hub/joshTracker syncKey rule "
                  "isn't published yet. Snapshot not saved; stopping quietly.")
            return
        raise
    print(f"Synced {len(clips)} clips ({untouched} untouched) to hub/joshTracker.")


if __name__ == "__main__":
    main()
