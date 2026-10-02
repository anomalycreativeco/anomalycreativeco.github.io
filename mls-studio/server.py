#!/usr/bin/env python3
"""
MLS Studio — the Studio Hub page for editing MLS photos.

One local server, no dependencies beyond the Python 3 that ships with macOS.
It drives the same AutoHDR API that the AutoHDR MCP server sits on top of,
resizes the finished photos for MLS with the house ladder (sips), and uploads
both sets to a Frame.io folder.

Run:   python3 server.py            (then open http://localhost:8765)
Env:   MLS_STUDIO_PORT=8765         MLS_STUDIO_DRYRUN=1 (simulate AutoHDR + Frame.io)
"""
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import ssl
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_VERSION = "1.7"  # bump whenever a route changes so an open page can ask for a restart
PORT = int(os.environ.get("MLS_STUDIO_PORT", "8765"))
CALLBACK_PORT = int(os.environ.get("MLS_STUDIO_CALLBACK_PORT", "8766"))  # HTTPS, Adobe requires https even on localhost
DRY_RUN = os.environ.get("MLS_STUDIO_DRYRUN", "") not in ("", "0", "false") or "--dry-run" in sys.argv

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = Path(os.environ.get("MLS_STUDIO_DATA_DIR") or (Path.home() / "Library" / "Application Support" / "Anomaly Studio Hub" / "mls-studio"))
CONFIG_PATH = DATA_DIR / "config.json"
JOBS_PATH = DATA_DIR / "jobs.json"
KEYCHAIN_SERVICE = "anomaly-studio-hub.mls-studio" + (".test" if os.environ.get("MLS_STUDIO_DATA_DIR") else "")
TLS_DIR = DATA_DIR / "tls"
HUB_ORIGINS = {"https://anomalycreativeco.github.io", "http://localhost:8790", "http://127.0.0.1:8790"}  # may ping /api/config and embed the page
REDIRECT_URI = "https://localhost:%d/callback" % CALLBACK_PORT

# --- AutoHDR external API (what the AutoHDR MCP server wraps) ---------------
AUTOHDR_TOKEN_URL = "https://new.autohdr.com/api/auth/oauth2/token"
AUTOHDR_API = "https://external.realestatephotoediting.com/v1"
AUTOHDR_SCOPES = "photoshoots:read photoshoots:write images:edit"

# --- Frame.io V4 + Adobe IMS -------------------------------------------------
FRAMEIO_API = "https://api.frame.io/v4"
IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
IMS_AUTHORIZE_URL = "https://ims-na1.adobelogin.com/ims/authorize/v2"
FRAMEIO_S2S_SCOPE = "openid AdobeID frame.s2s.all"
FRAMEIO_USER_SCOPE = "openid email profile offline_access additional_info.roles"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff"}
RAW_EXTS = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2", ".pef"}
ALL_EXTS = IMAGE_EXTS | RAW_EXTS

DEFAULT_CONFIG = {
    "autohdr_client_id": "",
    "frameio_mode": "user",           # "user" (OAuth Web App, any plan) or "s2s" (Server-to-Server, Enterprise plans only)
    "frameio_client_id": "",
    "frameio_token_expiry": 0,
    "frameio_account_id": "",
    "frameio_last_folder": None,      # {"account_id","workspace_id","project_id","folder_id","path"}
    "mls_limit_kb": 3999,
    "default_indoor_model_id": None,
    "default_outdoor_model_id": None,
    "last_source": "",
    "highres_folder_name": "High Res",
    "mls_folder_name": "MLS",
    "slack_notify": True,             # post to Slack after a Frame.io delivery
    "slack_channel": "#general",      # used with a bot token; a webhook already targets its channel
}

_lock = threading.RLock()


# ---------------------------------------------------------------- utilities --
def log(msg):
    print(datetime.now().strftime("%H:%M:%S"), msg, flush=True)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def natural_key(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def kc_get(key):
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", key, "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def kc_set(key, value):
    if not value:
        return kc_delete(key)
    subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", key, "-w", value],
                   check=True, capture_output=True)


def kc_delete(key):
    subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", key],
                   capture_output=True)


class ApiError(Exception):
    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


USER_AGENT = "MLSStudio/1.0 (Anomaly Studio Hub; +https://anomalycreativestudio.com)"


def http_call(method, url, headers=None, body=None, timeout=300):
    h = dict(headers or {})
    h.setdefault("User-Agent", USER_AGENT)  # api.frame.io returns an HTML 403 to Python's default agent
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def http_json(method, url, headers=None, json_body=None, form=None, timeout=120):
    h = dict(headers or {})
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode()
        h["Content-Type"] = "application/json"
    elif form is not None:
        body = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    h.setdefault("Accept", "application/json")
    status, data, hdrs = http_call(method, url, h, body, timeout)
    text = data.decode(errors="replace") if data else ""
    try:
        parsed = json.loads(text) if text else {}
    except ValueError:
        parsed = {"raw": text[:800]}
    return status, parsed, hdrs


def err_text(body):
    if isinstance(body, dict):
        return body.get("error_description") or body.get("message") or body.get("error") or body.get("raw") or json.dumps(body)[:300]
    return str(body)[:300]


# ------------------------------------------------------------- AutoHDR API --
class AutoHDR:
    def __init__(self, client_id, client_secret):
        if not client_id or not client_secret:
            raise ApiError("AutoHDR API app is not set up yet. Open Settings and add the client ID and secret.")
        self.client_id, self.client_secret = client_id, client_secret
        self._token, self._exp = None, 0

    def token(self):
        if self._token and time.time() < self._exp - 60:
            return self._token
        st, body, _ = http_json("POST", AUTOHDR_TOKEN_URL, form={
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": AUTOHDR_SCOPES,
            "resource": AUTOHDR_API,
        })
        if st != 200 or "access_token" not in body:
            raise ApiError("AutoHDR sign-in failed (%s): %s" % (st, err_text(body)), st, body)
        self._token = body["access_token"]
        self._exp = time.time() + int(body.get("expires_in", 3600))
        return self._token

    def call(self, method, path, json_body=None, params=None, ok=(200, 201, 202)):
        url = AUTOHDR_API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        for attempt in range(8):
            st, body, hdrs = http_json(method, url, {"Authorization": "Bearer " + self.token()}, json_body)
            if st == 429:
                time.sleep(min(int(hdrs.get("Retry-After", "5") or 5), 60))
                continue
            if st == 401 and attempt == 0:
                self._token = None
                continue
            if st in ok:
                return st, body
            raise ApiError("AutoHDR %s %s -> %s: %s" % (method, path, st, err_text(body)), st, body)
        raise ApiError("AutoHDR %s %s: still rate-limited after retries" % (method, path))

    def me(self):
        return self.call("GET", "/me")[1]

    def models(self):
        return self.call("GET", "/models", params={"type": "all"})[1].get("models", [])

    def capabilities(self):
        return self.call("GET", "/capabilities")[1].get("transforms", [])

    def create_photoshoot(self, address, files, indoor_model_id, outdoor_model_id, enhancements, kind):
        body = {"address": address, "files": [{"filename": f, "kind": kind} for f in files]}
        if indoor_model_id:
            body["indoor_model_id"] = int(indoor_model_id)
        if outdoor_model_id:
            body["outdoor_model_id"] = int(outdoor_model_id)
        if enhancements:
            body["enhancements"] = enhancements
        return self.call("POST", "/photoshoots", body)[1]

    @staticmethod
    def put_file(url, content_type, path):
        # Streamed from disk with an explicit Content-Length, so 50 RAW files in flight do not sit in memory.
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            st, body, _ = http_call("PUT", url, {"Content-Type": content_type, "Content-Length": str(size)}, f, timeout=3600)
        if st not in (200, 201, 204):
            text = body.decode(errors="replace")[:300] if body else ""
            if st == 403 and "expire" in text.lower():
                raise ApiError("Upload link for %s expired (AutoHDR links last 5 minutes). Try a smaller batch or a faster connection." % os.path.basename(path))
            raise ApiError("Upload of %s failed (%s): %s" % (os.path.basename(path), st, text))

    def commit(self, photoshoot_id, job_id):
        """Returns 'committed' or 'already'. Retries while files are still landing."""
        for _ in range(30):
            st, body = self.call("POST", "/photoshoots/%s/commit" % photoshoot_id, {"job_id": job_id}, ok=(200, 202, 409))
            if st in (200, 202):
                return "committed"
            if body.get("error") == "files_not_landed":
                time.sleep(5)
                continue
            return "already"  # 409 conflict = already committed
        raise ApiError("AutoHDR never saw the uploaded files land. Check the upload step and try again.")

    def status(self, photoshoot_id):
        return self.call("GET", "/photoshoots/%s/status" % photoshoot_id)[1]

    def photos(self, photoshoot_id):
        out, page = [], 0
        while True:
            body = self.call("GET", "/photoshoots/%s/photos" % photoshoot_id, params={"page": page, "page_size": 100})[1]
            out.extend(body.get("photos", []))
            page += 1
            if page >= int(body.get("page_count", 1) or 1):
                return out

    def download(self, photoshoot_id, items):
        return self.call("POST", "/photoshoots/%s/photos/download" % photoshoot_id, {"images": items})[1]

    def submit_transform(self, photoshoot_id, image_uuid, image_version_uuid, transform_name, **extra):
        body = {"photoshoot_id": int(photoshoot_id), "image_uuid": image_uuid, "image_version_uuid": image_version_uuid,
                "transform_name": transform_name}
        body.update({k: v for k, v in extra.items() if v is not None})
        return self.call("POST", "/transforms", body)[1]

    def transform_job(self, job_id):
        return self.call("GET", "/transforms/%s" % job_id)[1]


class DryAutoHDR:
    """Simulates AutoHDR so the whole pipeline can be exercised without spending credits."""
    _shoots = {}  # shared across instances: every step builds a fresh client

    def __init__(self, *a, **k):
        if not DryAutoHDR._shoots:
            DryAutoHDR._shoots = {int(k): v for k, v in load_json(DATA_DIR / "dry_shoots.json", {}).items()}

    def _save(self):
        save_json(DATA_DIR / "dry_shoots.json", DryAutoHDR._shoots)

    def me(self):
        return {"email": "dry-run@example.com", "credit_balance": 9999, "scopes": AUTOHDR_SCOPES.split()}

    def models(self):
        return [{"id": 1, "name": "Classic", "description": "Dry run", "type": "predefined", "variant": "indoor", "style_credit_cost": 1},
                {"id": 32, "name": "Blue Sky V4", "description": "Dry run", "type": "predefined", "variant": "outdoor", "style_credit_cost": 1},
                {"id": 43, "name": "Fuse", "description": "First ever true flambient model", "type": "custom", "variant": None, "style_credit_cost": 2}]

    def capabilities(self):
        return [{"name": "reedit", "display_name": "AI Re-edit", "credit_cost": 1}]

    def create_photoshoot(self, address, files, *a, **k):
        pid = int(time.time()) % 100000
        self._shoots[pid] = {"files": files, "t": time.time()}
        self._save()
        return {"photoshoot_id": pid, "job_id": str(uuid.uuid4()),
                "uploads": [{"filename": f, "url": "dry://" + f, "content_type": mimetypes.guess_type(f)[0] or "application/octet-stream"} for f in files]}

    @staticmethod
    def put_file(url, content_type, path):
        time.sleep(0.2)

    def commit(self, pid, job_id):
        return "committed"

    def status(self, pid):
        s = self._shoots.get(pid, {"t": 0, "files": []})
        done = time.time() - s["t"] > 8
        return {"status": "success" if done else "in_progress", "pipeline": "hdr", "image_count": len(s["files"]), "awaiting_files": False}

    def photos(self, pid):
        return [{"image_uuid": str(uuid.uuid4()), "image_version_uuid": str(uuid.uuid4()), "name": Path(f).stem + ".jpg", "_src": f}
                for f in self._shoots.get(pid, {"files": []})["files"]]

    def download(self, pid, items):
        return {"credits_charged": 0, "downloads": [{"image_uuid": i["image_uuid"], "filename": i["filename"], "url": "dry://" + i["filename"]} for i in items]}

    def submit_transform(self, *a, **k):
        return {"job_id": str(uuid.uuid4()), "status": "queued"}

    def transform_job(self, job_id):
        return {"status": "succeeded", "output_image_version_uuid": str(uuid.uuid4())}


# ------------------------------------------------------------- Frame.io API --
class FrameIO:
    def __init__(self, cfg):
        self.cfg = cfg
        self.mode = cfg.get("frameio_mode", "user")
        self.client_id = cfg.get("frameio_client_id", "")
        self.client_secret = kc_get("frameio_client_secret")
        if not self.client_id or not self.client_secret:
            raise ApiError("Frame.io is not set up yet. Open Settings and add the Adobe Developer Console client ID and secret.")
        self._token, self._exp = None, 0

    def token(self):
        if self.mode == "s2s":
            if self._token and time.time() < self._exp - 60:
                return self._token
            st, body, _ = http_json("POST", IMS_TOKEN_URL, form={
                "grant_type": "client_credentials", "client_id": self.client_id,
                "client_secret": self.client_secret, "scope": FRAMEIO_S2S_SCOPE})
            if st != 200 or "access_token" not in body:
                raise ApiError("Frame.io (Adobe IMS) sign-in failed (%s): %s" % (st, err_text(body)), st, body)
            self._token = body["access_token"]
            self._exp = time.time() + int(body.get("expires_in", 3600))
            return self._token
        # user mode: stored tokens from the Connect flow
        access = kc_get("frameio_access_token")
        expiry = float(self.cfg.get("frameio_token_expiry") or 0)
        if access and time.time() < expiry - 60:
            return access
        refresh = kc_get("frameio_refresh_token")
        if not refresh:
            raise ApiError("Frame.io is not connected. Open Settings and click Connect Frame.io.")
        st, body, _ = http_json("POST", IMS_TOKEN_URL, form={
            "grant_type": "refresh_token", "client_id": self.client_id,
            "client_secret": self.client_secret, "refresh_token": refresh})
        if st != 200 or "access_token" not in body:
            raise ApiError("Frame.io session expired and could not refresh (%s): %s. Reconnect in Settings." % (st, err_text(body)))
        store_frameio_tokens(body)
        return body["access_token"]

    def call(self, method, path, json_body=None, ok=(200, 201)):
        url = path if path.startswith("http") else FRAMEIO_API + path
        for attempt in range(8):
            st, body, hdrs = http_json(method, url, {"Authorization": "Bearer " + self.token()}, json_body)
            if st == 429:
                time.sleep(min(int(hdrs.get("Retry-After", "3") or 3), 60))
                continue
            if st == 401 and attempt == 0:
                self._token = None
                continue
            if st in ok:
                return body
            raise ApiError("Frame.io %s %s -> %s: %s" % (method, path.replace(FRAMEIO_API, ""), st, err_text(body)), st, body)
        raise ApiError("Frame.io %s %s: still rate-limited after retries" % (method, path))

    @staticmethod
    def unwrap(body):
        return body.get("data", body) if isinstance(body, dict) else body

    def list_all(self, path):
        out = []
        url = path
        while url:
            body = self.call("GET", url)
            out.extend(body.get("data", []))
            nxt = (body.get("links") or {}).get("next")
            if not nxt:
                break
            url = nxt if nxt.startswith("http") else ("https://api.frame.io" + nxt if nxt.startswith("/v4") else FRAMEIO_API + nxt)
        return out

    def me(self):
        return self.unwrap(self.call("GET", "/me"))

    def accounts(self):
        return self.list_all("/accounts?page_size=50")

    def workspaces(self, account_id):
        return self.list_all("/accounts/%s/workspaces?page_size=50" % account_id)

    def projects(self, account_id, workspace_id):
        return self.list_all("/accounts/%s/workspaces/%s/projects?page_size=50" % (account_id, workspace_id))

    def children(self, account_id, folder_id, thumbs=False):
        extra = "&include=media_links.thumbnail" if thumbs else ""
        return self.list_all("/accounts/%s/folders/%s/children?page_size=100%s" % (account_id, folder_id, extra))

    def project(self, account_id, project_id):
        return self.unwrap(self.call("GET", "/accounts/%s/projects/%s" % (account_id, project_id)))

    def resolve_link(self, url):
        """next.frame.io/project/<project>/<folder?> -> account, workspace, project, folder and its path."""
        parts = [x for x in urllib.parse.urlparse(url).path.split("/") if x and x != "view"]
        ids = parts[parts.index("project") + 1:parts.index("project") + 3] if "project" in parts else []
        if "frame.io" not in url or not ids:
            raise ApiError("That does not look like a next.frame.io project or folder link (expected next.frame.io/project/…).")
        project_id, folder_id = ids[0], (ids[1] if len(ids) > 1 else None)
        proj = account_id = None
        for a in self.accounts():
            try:
                proj = self.project(a["id"], project_id)
                account_id = a["id"]
                break
            except ApiError:
                continue
        if not proj:
            raise ApiError("No project with that id in your Frame.io accounts. Is the link from a workspace you are a member of?")
        root = proj["root_folder_id"]
        trail = [{"id": root, "name": proj["name"]}]
        if folder_id and folder_id != root:
            chain, cur, guard = [], folder_id, 0
            while cur and cur != root and guard < 30:
                f = self.folder(account_id, cur)
                if f.get("type") == "file":
                    raise ApiError("That link points at a file. Paste the link of the folder it should go in.")
                chain.append({"id": f["id"], "name": f.get("name") or "folder"})
                cur = f.get("parent_id")
                guard += 1
            trail += list(reversed(chain))
        return {"account_id": account_id, "workspace_id": proj.get("workspace_id"), "project_id": proj["id"],
                "folder_id": trail[-1]["id"], "path": " / ".join(t["name"] for t in trail), "trail": trail}

    def folder(self, account_id, folder_id):
        return self.unwrap(self.call("GET", "/accounts/%s/folders/%s" % (account_id, folder_id)))

    def create_folder(self, account_id, parent_id, name):
        return self.unwrap(self.call("POST", "/accounts/%s/folders/%s/folders" % (account_id, parent_id), {"data": {"name": name}}))

    def find_or_create_folder(self, account_id, parent_id, name):
        for c in self.children(account_id, parent_id):
            if c.get("type") == "folder" and c.get("name") == name:
                return c, False
        return self.create_folder(account_id, parent_id, name), True

    def upload_file(self, account_id, folder_id, path, existing_names=None):
        path = Path(path)
        if existing_names is not None and path.name in existing_names:
            return None, "skipped"
        size = path.stat().st_size
        created = self.unwrap(self.call("POST", "/accounts/%s/folders/%s/files/local_upload" % (account_id, folder_id),
                                        {"data": {"name": path.name, "file_size": size}}))
        media_type = created.get("media_type") or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            for part in created.get("upload_urls", []):
                chunk = f.read(int(part["size"]))
                st, body, _ = http_call("PUT", part["url"], {"x-amz-acl": "private", "Content-Type": media_type}, chunk, timeout=900)
                if st not in (200, 201, 204):
                    raise ApiError("Frame.io chunk upload of %s failed (%s): %s" % (path.name, st, (body or b"").decode(errors="replace")[:200]))
        return created, "uploaded"


class DryFrameIO:
    def __init__(self, cfg):
        self.cfg = cfg
        self.mode = "dry"

    def me(self):
        return {"name": "Dry run", "email": "dry-run@example.com"}

    def accounts(self):
        return [{"id": "acct-dry", "display_name": "Dry-run account"}]

    def workspaces(self, a):
        return [{"id": "ws-dry", "name": "Dry-run workspace"}]

    TREE = {"root-dry": ["Delivered", "Raw", "Social"], "root-dry2": ["Listings"], "dry-delivered": ["2026-09", "2026-10"], "dry-2026-10": ["214 Hawthorne Ln"], "dry-listings": ["Twilights"]}

    def projects(self, a, w):
        return [{"id": "proj-dry", "name": "Beechwood Homes", "root_folder_id": "root-dry", "workspace_id": "ws-dry"}, {"id": "proj-dry2", "name": "Bannister Properties", "root_folder_id": "root-dry2", "workspace_id": "ws-dry"}] + \
               [{"id": "proj-dry-%d" % i, "name": n, "root_folder_id": "root-dry-%d" % i, "workspace_id": "ws-dry"} for i, n in enumerate(("777 Team", "8M Solar", "All Pro", "Amish Roots", "Ares Digital", "Becoming Her", "Big Hitters", "Blueprint", "Currin Outdoor", "Exeter", "Fitclub", "Hawthorne", "Lakeside Pointe", "Marlowe", "Moriarty PT", "Renew", "Revolution Homes", "Shadow Creek", "SuperFitness", "Vellure"))]

    def children(self, a, f, thumbs=False):
        return [{"id": "dry-" + re.sub(r"\W+", "-", n).lower(), "name": n, "type": "folder"} for n in self.TREE.get(f, [])] + ([{"id": "dry-file", "name": "cover.jpg", "type": "file", "file_size": 1234567}] if f == "dry-2026-10" else [])

    def project(self, a, pid):
        return next(p for p in self.projects(a, None) if p["id"] == pid)

    def folder(self, a, f):
        parent = next((k for k, v in self.TREE.items() if any("dry-" + re.sub(r"\W+", "-", n).lower() == f for n in v)), None)
        name = next((n for v in self.TREE.values() for n in v if "dry-" + re.sub(r"\W+", "-", n).lower() == f), f)
        return {"id": f, "name": name, "parent_id": parent, "type": "folder", "view_url": "https://next.frame.io/"}

    resolve_link = FrameIO.resolve_link
    unwrap = staticmethod(FrameIO.unwrap)


    def folder(self, a, f):
        return {"id": f, "name": "Dry-run folder", "view_url": "https://next.frame.io/"}

    def find_or_create_folder(self, a, p, name):
        return {"id": "dry-" + re.sub(r"\W+", "-", name).lower(), "name": name, "view_url": "https://next.frame.io/"}, True

    def upload_file(self, a, folder_id, path, existing_names=None):
        time.sleep(0.1)
        return {"id": "dry", "name": Path(path).name}, "uploaded"


def store_frameio_tokens(body):
    kc_set("frameio_access_token", body.get("access_token", ""))
    if body.get("refresh_token"):
        kc_set("frameio_refresh_token", body["refresh_token"])
    with _lock:
        cfg = get_config()
        cfg["frameio_token_expiry"] = time.time() + int(body.get("expires_in", 3600))
        save_json(CONFIG_PATH, cfg)


# ------------------------------------------------------------- MLS resizing --
SCALES = (100, 90, 80, 70, 60, 50, 40, 30)
QUALITIES = (92, 86, 80, 74, 68, 62)


def sips_dims(path):
    r = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)], capture_output=True, text=True)
    w = h = 0
    for line in r.stdout.splitlines():
        if "pixelWidth" in line:
            w = int(line.split()[-1])
        elif "pixelHeight" in line:
            h = int(line.split()[-1])
    return w, h


def mls_resize_one(src, dest, limit_kb, tmpdir):
    """Same ladder as the mls-resizing skill: full res first, exhaust quality before stepping down."""
    src, dest = Path(src), Path(dest)
    limit = int(limit_kb) * 1024
    size = src.stat().st_size
    if src.suffix.lower() in (".jpg", ".jpeg") and size <= limit:
        shutil.copy2(src, dest)
        w, h = sips_dims(dest)
        return {"kb": size // 1024, "pixels": "%dx%d" % (w, h), "quality": "original"}
    w, h = sips_dims(src)
    long_edge = max(w, h) or 1
    tmp = Path(tmpdir) / ("%s.jpg" % uuid.uuid4().hex)
    for s in SCALES:
        px = long_edge * s // 100
        for q in QUALITIES:
            if tmp.exists():
                tmp.unlink()
            args = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(q)]
            if s < 100:
                args += ["-Z", str(px)]
            args += [str(src), "--out", str(tmp)]
            subprocess.run(args, capture_output=True)
            if not tmp.exists():
                continue
            sz = tmp.stat().st_size
            if sz <= limit:
                shutil.move(str(tmp), str(dest))
                ow, oh = sips_dims(dest)
                return {"kb": sz // 1024, "pixels": "%dx%d" % (ow, oh), "quality": "q%d" % q}
    raise ApiError("%s cannot get under %s KB even at the smallest rung" % (src.name, limit_kb))


# -------------------------------------------------------------- job engine --
JOBS = {}          # id -> job dict
RUNNERS = {}       # id -> thread
CANCEL = set()


def get_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(CONFIG_PATH, {}))
    return cfg


def public_config():
    cfg = get_config()
    cfg["secrets"] = {
        "autohdr_client_secret": bool(kc_get("autohdr_client_secret")),
        "frameio_client_secret": bool(kc_get("frameio_client_secret")),
        "frameio_connected": bool(kc_get("frameio_refresh_token")),
        "slack_webhook_url": bool(kc_get("slack_webhook_url")),
        "slack_bot_token": bool(kc_get("slack_bot_token")),
    }
    cfg["dry_run"] = DRY_RUN
    cfg["version"] = APP_VERSION
    cfg["port"] = PORT
    cfg["frameio_redirect_uri"] = REDIRECT_URI
    return cfg


def autohdr_client():
    if DRY_RUN:
        return DryAutoHDR()
    cfg = get_config()
    return AutoHDR(cfg.get("autohdr_client_id"), kc_get("autohdr_client_secret"))


def frameio_client():
    if DRY_RUN:
        return DryFrameIO(get_config())
    return FrameIO(get_config())


def scan_folder(path):
    p = Path(path).expanduser()
    if not p.is_dir():
        raise ApiError("Not a folder: %s" % path)
    visible = [f for f in p.iterdir() if f.is_file() and not f.name.startswith(".")]
    files = [f for f in visible if f.suffix.lower() in ALL_EXTS]
    files.sort(key=lambda f: natural_key(f.name))
    skipped = {}
    for f in visible:
        if f.suffix.lower() not in ALL_EXTS:
            skipped[f.suffix.lstrip(".").upper() or "no extension"] = skipped.get(f.suffix.lstrip(".").upper() or "no extension", 0) + 1
    raw = sum(1 for f in files if f.suffix.lower() in RAW_EXTS)
    total_bytes = sum(f.stat().st_size for f in files)
    return {
        "path": str(p), "name": p.name, "count": len(files), "raw_count": raw, "total_mb": round(total_bytes / 1048576, 1),
        "files": [{"name": f.name, "bytes": f.stat().st_size} for f in files],
        "subfolders": sorted(d.name for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")),
        "skipped": skipped,  # e.g. {"MP4": 2}: videos and other non-photo files are not sent to AutoHDR
    }


def persist_jobs():
    with _lock:
        save_json(JOBS_PATH, {"jobs": list(JOBS.values())})


def jlog(job, msg):
    line = "%s  %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    with _lock:
        job["log"].append(line)
        job["log"] = job["log"][-400:]
        job["updated"] = time.time()
    log("[%s] %s" % (job["name"], msg))
    persist_jobs()


def jset(job, **kw):
    with _lock:
        job.update(kw)
        job["updated"] = time.time()
    persist_jobs()


def plan_steps(job):
    o = job["options"]
    steps = []
    if o.get("do_autohdr"):
        steps += ["create", "upload", "commit", "process"]
        if (o.get("reedit_prompt") or "").strip():
            steps.append("reedit")
        steps.append("download")
    if o.get("do_resize"):
        steps.append("resize")
    if o.get("do_frameio"):
        steps.append("frameio")
        steps.append("notify")
    steps.append("done")
    return steps


def check_cancel(job):
    if job["id"] in CANCEL:
        raise ApiError("Cancelled")


def run_job(job):
    steps = job.get("steps") or plan_steps(job)
    cfg = get_config()
    try:
        jset(job, status="running", error=None)
        idx = job.get("step_index", 0)
        # an interrupted upload cannot resume: AutoHDR upload links last 5 minutes
        if idx < len(steps) and steps[idx] == "upload":
            idx = steps.index("create")
            job["results"].pop("photoshoot_id", None)
        while idx < len(steps):
            step = steps[idx]
            jset(job, step=step, step_index=idx, progress={"current": 0, "total": 0, "label": ""})
            check_cancel(job)
            globals()["step_" + step](job, cfg)
            idx += 1
        jset(job, status="done", step="done", step_index=len(steps) - 1, finished=time.time())
        jlog(job, "Done.")
    except ApiError as e:
        if str(e) == "Cancelled":
            jset(job, status="cancelled")
            jlog(job, "Cancelled.")
        else:
            jset(job, status="failed", error=str(e))
            jlog(job, "FAILED: %s" % e)
    except Exception as e:  # noqa
        jset(job, status="failed", error="%s: %s" % (type(e).__name__, e))
        jlog(job, "FAILED: %s: %s" % (type(e).__name__, e))
    finally:
        CANCEL.discard(job["id"])


def source_files(job):
    return [Path(job["source"]) / f for f in job["files"]]


def output_root(job):
    return Path(job["source"]) / "_MLS Studio"


def step_create(job, cfg):
    o = job["options"]
    hdr = autohdr_client()
    kind = o.get("kind", "raw")
    jlog(job, "Creating AutoHDR photoshoot '%s' (%d files, %s)" % (job["name"], len(job["files"]), "HDR pipeline" if kind == "raw" else "finished photos, no HDR"))
    enh = o.get("enhancements") or {}
    enhancements = {k: True for k in ("grass", "declutter", "fireplace") if enh.get(k)}
    if enh.get("tv_replacement"):
        enhancements["tv_replacement"] = {"enabled": True, "mode": enh.get("tv_mode") or "blackout"}
    resp = hdr.create_photoshoot(job["name"], job["files"], o.get("indoor_model_id"), o.get("outdoor_model_id"), enhancements or None, kind)
    job["results"]["photoshoot_id"] = resp["photoshoot_id"]
    job["results"]["ingest_job_id"] = resp["job_id"]
    job["results"]["uploads"] = resp.get("uploads", [])
    jset(job)
    jlog(job, "Photoshoot %s created." % resp["photoshoot_id"])


def step_upload(job, cfg):
    hdr = autohdr_client()
    uploads = job["results"].get("uploads", [])
    by_name = {u["filename"]: u for u in uploads}
    files = source_files(job)
    total = len(files)
    done = [0]
    workers = max(1, min(total, 48))
    jlog(job, "Uploading %d files (%s MB) to AutoHDR, all at once so every upload starts before the 5-minute links expire" % (total, round(sum(f.stat().st_size for f in files) / 1048576)))
    jset(job, progress={"current": 0, "total": total, "label": "Uploading"})

    def one(f):
        check_cancel(job)
        u = by_name.get(f.name)
        if not u:
            raise ApiError("AutoHDR returned no upload link for %s" % f.name)
        hdr.put_file(u["url"], u.get("content_type") or mimetypes.guess_type(f.name)[0] or "application/octet-stream", f)
        with _lock:
            done[0] += 1
            job["progress"] = {"current": done[0], "total": total, "label": "Uploading"}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, files))
    jlog(job, "Upload complete.")


def step_commit(job, cfg):
    hdr = autohdr_client()
    r = hdr.commit(job["results"]["photoshoot_id"], job["results"]["ingest_job_id"])
    jlog(job, "Committed — AutoHDR is processing." if r == "committed" else "Already committed; resuming.")
    job["results"]["committed_at"] = time.time()
    jset(job)


def step_process(job, cfg):
    hdr = autohdr_client()
    pid = job["results"]["photoshoot_id"]
    start = job["results"].get("committed_at") or time.time()
    failures = 0
    while True:
        check_cancel(job)
        s = hdr.status(pid)
        elapsed = int(time.time() - start)
        label = "AutoHDR processing · %d photos so far · %d:%02d elapsed" % (int(s.get("image_count") or 0), elapsed // 60, elapsed % 60)
        jset(job, progress={"current": int(s.get("image_count") or 0), "total": 0, "label": label})
        if s.get("awaiting_files"):
            jlog(job, "AutoHDR is still waiting for files; re-committing.")
            hdr.commit(pid, job["results"]["ingest_job_id"])
        elif s.get("status") == "success":
            job["results"]["image_count"] = s.get("image_count")
            jset(job)
            jlog(job, "AutoHDR finished: %s photos (%s pipeline)." % (s.get("image_count"), s.get("pipeline")))
            return
        elif s.get("status") == "failure":
            failures += 1
            if failures > 1:
                raise ApiError("AutoHDR reported the shoot as failed.")
            jlog(job, "AutoHDR flagged a failure; waiting 3 minutes and checking once more (slow shoots sometimes recover).")
            time.sleep(3 if DRY_RUN else 180)
            continue
        if elapsed > 3 * 3600:
            raise ApiError("AutoHDR has been processing for over 3 hours. Check the shoot in the AutoHDR app, then Resume.")
        time.sleep(2 if DRY_RUN else 20)


def step_reedit(job, cfg):
    hdr = autohdr_client()
    pid = job["results"]["photoshoot_id"]
    prompt = job["options"]["reedit_prompt"].strip()
    photos = hdr.photos(pid)
    pending = job["results"].get("reedit_jobs") or {}
    jlog(job, "Re-edit prompt on %d photos (1 credit each): \"%s\"" % (len(photos), prompt))
    for p in photos:
        check_cancel(job)
        if p["image_uuid"] in pending:
            continue
        r = hdr.submit_transform(pid, p["image_uuid"], p["image_version_uuid"], "reedit", prompt=prompt)
        pending[p["image_uuid"]] = {"job_id": r["job_id"], "status": r.get("status")}
        job["results"]["reedit_jobs"] = pending
        jset(job, progress={"current": len(pending), "total": len(photos), "label": "Submitting re-edits"})
    while True:
        check_cancel(job)
        open_ = [k for k, v in pending.items() if v.get("status") not in ("succeeded", "failed")]
        jset(job, progress={"current": len(pending) - len(open_), "total": len(pending), "label": "Re-edits running"})
        if not open_:
            break
        for k in open_:
            r = hdr.transform_job(pending[k]["job_id"])
            pending[k]["status"] = r.get("status")
            if r.get("status") == "failed":
                pending[k]["error"] = r.get("failure_message")
        job["results"]["reedit_jobs"] = pending
        jset(job)
        time.sleep(1 if DRY_RUN else 10)
    failed = [k for k, v in pending.items() if v.get("status") == "failed"]
    jlog(job, "Re-edits finished%s." % (" (%d failed, refunded, original kept)" % len(failed) if failed else ""))


def step_download(job, cfg):
    hdr = autohdr_client()
    pid = job["results"]["photoshoot_id"]
    photos = hdr.photos(pid)
    if not photos:
        raise ApiError("AutoHDR returned no photos for shoot %s." % pid)
    out = output_root(job) / cfg.get("highres_folder_name", "High Res")
    out.mkdir(parents=True, exist_ok=True)
    items = []
    for p in photos:
        name = p.get("name") or p["image_uuid"]
        if not name.lower().endswith((".jpg", ".jpeg", ".png")):
            name = Path(name).stem + ".jpg"
        items.append({"image_uuid": p["image_uuid"], "filename": name, "_src": p.get("_src")})
    jlog(job, "Downloading %d finished photos (1 credit per first-time download on house models)" % len(items))
    jset(job, progress={"current": 0, "total": len(items), "label": "Downloading"})
    saved = []
    srcmap = {i["image_uuid"]: i.get("_src") for i in items}
    for i in range(0, len(items), 100):
        batch = [{"image_uuid": x["image_uuid"], "filename": x["filename"]} for x in items[i:i + 100]]
        resp = hdr.download(pid, batch)
        for d in resp.get("downloads", []):
            check_cancel(job)
            dest = out / d["filename"]
            if DRY_RUN:
                shutil.copy2(Path(job["source"]) / srcmap[d["image_uuid"]], dest)
            else:
                with urllib.request.urlopen(d["url"], timeout=600) as r, open(dest, "wb") as f:
                    shutil.copyfileobj(r, f)
            saved.append(str(dest))
            jset(job, progress={"current": len(saved), "total": len(items), "label": "Downloading"})
        if resp.get("credits_charged") is not None:
            job["results"]["credits_charged"] = (job["results"].get("credits_charged") or 0) + int(resp.get("credits_charged") or 0)
            jlog(job, "Credits charged for this batch: %s" % resp.get("credits_charged"))
    job["results"]["highres_dir"] = str(out)
    job["results"]["highres_files"] = saved
    jset(job)
    jlog(job, "Saved %d high-res photos to %s" % (len(saved), out))


def step_resize(job, cfg):
    o = job["options"]
    limit_kb = int(o.get("mls_limit_kb") or cfg.get("mls_limit_kb") or 3999)
    if job["results"].get("highres_files"):
        inputs = [Path(p) for p in job["results"]["highres_files"]]
    else:
        inputs = [f for f in source_files(job) if f.suffix.lower() in IMAGE_EXTS]
        skipped = len(job["files"]) - len(inputs)
        if skipped:
            jlog(job, "Skipping %d RAW files in MLS resize (resize works on finished JPEG/PNG/HEIC/TIFF)." % skipped)
    out = output_root(job) / cfg.get("mls_folder_name", "MLS")
    out.mkdir(parents=True, exist_ok=True)
    jlog(job, "Resizing %d photos to under %d KB -> %s" % (len(inputs), limit_kb, out))
    results, failed = [], []
    with tempfile.TemporaryDirectory() as tmp:
        for i, src in enumerate(inputs, 1):
            check_cancel(job)
            dest = out / (src.stem + ".jpg")
            try:
                r = mls_resize_one(src, dest, limit_kb, tmp)
                r["name"] = src.name
                results.append(r)
            except ApiError as e:
                failed.append(src.name)
                jlog(job, "FAILED to resize %s: %s" % (src.name, e))
            jset(job, progress={"current": i, "total": len(inputs), "label": "Resizing for MLS"})
    job["results"]["mls_dir"] = str(out)
    job["results"]["mls_files"] = [str(out / (Path(r["name"]).stem + ".jpg")) for r in results]
    job["results"]["resize_report"] = results
    job["results"]["resize_failed"] = failed
    jset(job)
    jlog(job, "MLS set ready: %d resized%s." % (len(results), ", %d FAILED" % len(failed) if failed else ""))


def step_frameio(job, cfg):
    fio = frameio_client()
    dest = job["frameio"]
    acct, folder_id = dest["account_id"], dest["folder_id"]
    highres = [Path(p) for p in job["results"].get("highres_files") or []] or [f for f in source_files(job)]
    mls = [Path(p) for p in job["results"].get("mls_files") or []]
    parent = folder_id
    if dest.get("create_shoot_folder", True):
        shoot, created = fio.find_or_create_folder(acct, folder_id, job["name"])
        parent = shoot["id"]
        jlog(job, "%s Frame.io folder '%s'" % ("Created" if created else "Using existing", job["name"]))
        job["results"]["frameio_shoot_url"] = shoot.get("view_url")
    sets = [(cfg.get("highres_folder_name", "High Res"), highres)]
    if mls:
        sets.append((cfg.get("mls_folder_name", "MLS"), mls))
    total = sum(len(s[1]) for s in sets)
    done = 0
    links = {}
    for name, files in sets:
        folder, created = fio.find_or_create_folder(acct, parent, name)
        links[name] = folder.get("view_url")
        existing = {c.get("name") for c in fio.children(acct, folder["id"]) if c.get("type") == "file"}
        uploaded = skipped = 0
        jlog(job, "Uploading %d files to Frame.io › %s / %s" % (len(files), job["name"] if dest.get("create_shoot_folder", True) else dest.get("path", ""), name))
        for f in files:
            check_cancel(job)
            _, state = fio.upload_file(acct, folder["id"], f, existing)
            if state == "uploaded":
                uploaded += 1
            else:
                skipped += 1
            done += 1
            jset(job, progress={"current": done, "total": total, "label": "Uploading to Frame.io · " + name})
        jlog(job, "%s: %d uploaded%s." % (name, uploaded, ", %d already there (skipped)" % skipped if skipped else ""))
    job["results"]["frameio_links"] = links
    jset(job)


def slack_configured():
    return bool(kc_get("slack_webhook_url") or kc_get("slack_bot_token"))


def slack_post(text, blocks=None, cfg=None):
    """Incoming webhook first (channel fixed at creation); bot token + channel as the alternative."""
    cfg = cfg or get_config()
    webhook, token = kc_get("slack_webhook_url"), kc_get("slack_bot_token")
    payload = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    if webhook:
        st, body, _ = http_json("POST", webhook, json_body=payload)
        if st != 200:
            raise ApiError("Slack webhook refused the message (%s): %s" % (st, err_text(body)))
        return "webhook"
    if token:
        payload["channel"] = cfg.get("slack_channel") or "#general"
        st, body, _ = http_json("POST", "https://slack.com/api/chat.postMessage", {"Authorization": "Bearer " + token}, json_body=payload)
        if st != 200 or not body.get("ok"):
            raise ApiError("Slack rejected the message: %s" % (body.get("error") or err_text(body)))
        return "bot"
    raise ApiError("Slack is not set up. Open Settings and add an incoming webhook URL for #general.")


def slack_delivery_message(job, cfg):
    o, r = job["options"], job["results"]
    hi, mls = len(r.get("highres_files") or job["files"]), len(r.get("mls_files") or [])
    links = r.get("frameio_links") or {}
    dest = job.get("frameio") or {}
    trail = dest.get("trail") or []
    # In this workspace every Frame.io project is a client, so the project name is the client name.
    client = dest.get("project_name") or (trail[0].get("name") if trail else None) or "Unknown client"
    where = dest.get("path", "Frame.io")
    if dest.get("create_shoot_folder", True):
        where += " / " + job["name"]
    parts = []
    if o.get("do_autohdr"):
        looks = [n for n in (o.get("indoor_model_name"), o.get("outdoor_model_name")) if n]
        parts.append("Edited with AutoHDR" + (" (%s)" % " / ".join(looks) if looks else "") + (" · HDR" if o.get("kind", "raw") == "raw" else " · finished photos"))
        if (o.get("reedit_prompt") or "").strip():
            parts.append("Re-edit: \u201c%s\u201d" % o["reedit_prompt"].strip())
    if mls:
        parts.append("MLS set under %s KB" % (o.get("mls_limit_kb") or cfg.get("mls_limit_kb") or 3999))
    link_bits = []
    if r.get("frameio_shoot_url"):
        link_bits.append("<%s|Open the shoot folder>" % r["frameio_shoot_url"])
    for name in (cfg.get("highres_folder_name", "High Res"), cfg.get("mls_folder_name", "MLS")):
        if links.get(name):
            link_bits.append("<%s|%s>" % (links[name], name))
    failed = r.get("resize_failed") or []
    text = "Delivered for %s: %s — %d High Res%s" % (client, job["name"], hi, (" + %d MLS" % mls) if mls else "")
    fields = "*Client:* %s\n*Shoot:* %s\n*%d* High Res%s uploaded to *%s*" % (client, job["name"], hi, (" + *%d* MLS" % mls) if mls else "", where)
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": ("Delivered for %s" % client)[:150], "emoji": False}},
        {"type": "section", "text": {"type": "mrkdwn", "text": fields + ("\n" + " · ".join(parts) if parts else "")}},
    ]
    if link_bits:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "  ·  ".join(link_bits)}})
    ctx = []
    if r.get("photoshoot_id"):
        ctx.append("AutoHDR shoot #%s" % r["photoshoot_id"])
    if r.get("credits_charged") is not None:
        ctx.append("%s credits" % r["credits_charged"])
    started, finished = job.get("created"), time.time()
    ctx.append("%d min · MLS Studio" % max(1, int((finished - started) / 60)))
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "  ·  ".join(ctx)}]})
    if failed:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": ":warning: %d photo%s could not get under the MLS limit: %s" % (len(failed), "" if len(failed) == 1 else "s", ", ".join(failed))}})
    return text, blocks


def step_notify(job, cfg):
    if not cfg.get("slack_notify", True) or not slack_configured():
        jlog(job, "Slack not set up; skipping the notification.")
        return
    text, blocks = slack_delivery_message(job, cfg)
    try:
        via = slack_post(text, blocks, cfg)
        job["results"]["slack_posted"] = True
        jset(job)
        jlog(job, "Posted the delivery note to Slack (%s)." % ("#general via webhook" if via == "webhook" else cfg.get("slack_channel", "#general")))
    except ApiError as e:
        job["results"]["slack_error"] = str(e)
        jset(job)
        jlog(job, "WARNING: Slack message failed (delivery itself is fine): %s" % e)


def step_done(job, cfg):
    pass


def start_job(job):
    t = threading.Thread(target=run_job, args=(job,), daemon=True)
    RUNNERS[job["id"]] = t
    t.start()


# ----------------------------------------------------------------- server --
def choose_folder():
    script = ('tell application "System Events" to activate\n'
              'set f to choose folder with prompt "Choose the shoot folder"\n'
              'POSIX path of f')
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return r.stdout.strip().rstrip("/")


def exchange_frameio_code(q):
    cfg = get_config()
    if q.get("error"):
        raise ApiError("Adobe sign-in returned %s: %s" % (q.get("error"), q.get("error_description", "")))
    if q.get("state") != cfg.get("frameio_oauth_state") or not q.get("code"):
        raise ApiError("Frame.io sign-in was rejected or the state did not match. Try Connect again.")
    st, body, _ = http_json("POST", IMS_TOKEN_URL, form={
        "grant_type": "authorization_code", "client_id": cfg["frameio_client_id"],
        "client_secret": kc_get("frameio_client_secret") or "", "code": q["code"]})
    if st != 200 or "access_token" not in body:
        raise ApiError("Frame.io token exchange failed (%s): %s" % (st, err_text(body)))
    store_frameio_tokens(body)


def ensure_tls_cert():
    """Self-signed localhost cert for the OAuth callback. Adobe IMS only accepts https redirect URIs."""
    TLS_DIR.mkdir(parents=True, exist_ok=True)
    cert, key = TLS_DIR / "cert.pem", TLS_DIR / "key.pem"
    if not (cert.exists() and key.exists()):
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
                        "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True, capture_output=True)
        os.chmod(key, 0o600)
    return cert, key


CALLBACK_PAGE = """<!doctype html><meta charset=utf-8><title>MLS Studio</title>
<body style="font-family:-apple-system,sans-serif;background:#101415;color:#EEF1F1;display:grid;place-items:center;height:100vh;margin:0">
<div style="max-width:420px;text-align:center"><h2 style="color:%s">%s</h2><p>%s</p><p><a href="http://localhost:%d/" style="color:#8FB3AA">Back to MLS Studio</a></p></div>"""


class CallbackHandler(BaseHTTPRequestHandler):
    """HTTPS listener that only receives the Adobe sign-in redirect."""
    def log_message(self, fmt, *args):
        log("callback %s" % self.path.split("?")[0])

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path != "/callback":
            self.send_error(404)
            return
        try:
            exchange_frameio_code(q)
            self.send_response(302)
            self.send_header("Location", "http://localhost:%d/?frameio=connected" % PORT)
            self.end_headers()
        except Exception as e:  # noqa
            body = (CALLBACK_PAGE % ("#D9A273", "Frame.io sign-in failed", str(e).replace("<", "&lt;"), PORT)).encode()
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def start_callback_server():
    try:
        cert, key = ensure_tls_cert()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        srv = ThreadingHTTPServer(("127.0.0.1", CALLBACK_PORT), CallbackHandler)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log("Frame.io sign-in callback on %s" % REDIRECT_URI)
    except Exception as e:  # noqa
        log("WARNING: could not start the HTTPS callback listener (%s). Frame.io Connect will not work until this is fixed." % e)


class Handler(BaseHTTPRequestHandler):
    server_version = "MLSStudio/" + APP_VERSION

    def log_message(self, fmt, *args):  # quieter console
        if "/api/jobs" not in self.path or self.command != "GET":
            log("%s %s" % (self.command, self.path))

    # helpers
    def cors(self):
        origin = self.headers.get("Origin") or ""
        if origin in HUB_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Private-Network", "true")  # https hub page -> http://localhost
            self.send_header("Vary", "Origin")

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def send_file(self, path):
        p = (STATIC_DIR / path).resolve()
        if not str(p).startswith(str(STATIC_DIR)) or not p.is_file():
            self.send_error(404)
            return
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(p))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def do_GET(self):
        try:
            self.route("GET")
        except ApiError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:  # noqa
            self.send_json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    def do_POST(self):
        try:
            self.route("POST")
        except ApiError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:  # noqa
            self.send_json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    def route(self, method):
        u = urllib.parse.urlparse(self.path)
        path, q = u.path, dict(urllib.parse.parse_qsl(u.query))
        if method == "GET":
            if path in ("/", "/index.html"):
                return self.send_file("index.html")
            if path.startswith("/static/"):
                return self.send_file(path[len("/static/"):])
            if path == "/api/config":
                return self.send_json(public_config())
            if path == "/api/autohdr/models":
                hdr = autohdr_client()
                return self.send_json({"models": hdr.models(), "transforms": hdr.capabilities()})
            if path == "/api/autohdr/me":
                return self.send_json(autohdr_client().me())
            if path == "/api/frameio/me":
                fio = frameio_client()
                return self.send_json({"me": fio.me(), "accounts": fio.accounts()})
            if path == "/api/frameio/accounts":
                return self.send_json({"accounts": frameio_client().accounts()})
            if path == "/api/frameio/workspaces":
                return self.send_json({"workspaces": frameio_client().workspaces(q["account_id"])})
            if path == "/api/frameio/projects":
                return self.send_json({"projects": frameio_client().projects(q["account_id"], q["workspace_id"])})
            if path == "/api/frameio/children":
                kids = frameio_client().children(q["account_id"], q["folder_id"], thumbs=True)
                files = []
                for c in kids:
                    if c.get("type") == "folder":
                        continue
                    thumb = ((c.get("media_links") or {}).get("thumbnail") or {}).get("download_url")
                    files.append({"id": c["id"], "name": c.get("name"), "type": c.get("type"), "thumb": thumb, "bytes": c.get("file_size")})
                return self.send_json({"folders": [{"id": c["id"], "name": c.get("name")} for c in kids if c.get("type") == "folder"],
                                       "files": files, "file_count": len(files)})
            if path == "/api/frameio/connect":
                return self.frameio_connect()
            if path == "/api/frameio/callback":
                return self.frameio_callback(q)
            if path == "/api/jobs":
                with _lock:
                    jobs = sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)
                return self.send_json({"jobs": jobs})
            m = re.match(r"^/api/jobs/([\w-]+)$", path)
            if m:
                job = JOBS.get(m.group(1))
                return self.send_json(job) if job else self.send_json({"error": "No such job"}, 404)
            return self.send_error(404)

        body = self.read_json()
        if path == "/api/config":
            return self.save_config(body)
        if path == "/api/frameio/resolve":
            return self.send_json(frameio_client().resolve_link((body.get("url") or "").strip()))
        if path == "/api/pick-folder":
            p = choose_folder()
            return self.send_json({"path": p, "cancelled": p is None})
        if path == "/api/scan":
            info = scan_folder(body.get("path", ""))
            with _lock:
                cfg = get_config()
                cfg["last_source"] = info["path"]
                save_json(CONFIG_PATH, cfg)
            return self.send_json(info)
        if path == "/api/slack/test":
            via = slack_post("MLS Studio is connected. Delivery notes will land here once a shoot is uploaded to Frame.io.",
                             [{"type": "section", "text": {"type": "mrkdwn", "text": ":white_check_mark: *MLS Studio is connected.* Delivery notes will land here once a shoot is uploaded to Frame.io."}}])
            return self.send_json({"ok": True, "via": via})
        if path == "/api/open":
            subprocess.run(["open", body.get("path", "")])
            return self.send_json({"ok": True})
        if path == "/api/jobs":
            return self.create_job(body)
        m = re.match(r"^/api/jobs/([\w-]+)/(cancel|resume|delete)$", path)
        if m:
            job = JOBS.get(m.group(1))
            if not job:
                return self.send_json({"error": "No such job"}, 404)
            action = m.group(2)
            if action == "cancel":
                CANCEL.add(job["id"])
                jlog(job, "Cancel requested; stopping after the current file.")
            elif action == "resume":
                if job["status"] in ("running",):
                    raise ApiError("Job is already running.")
                jlog(job, "Resuming from step '%s'." % job.get("step"))
                start_job(job)
            elif action == "delete":
                if job["status"] == "running":
                    raise ApiError("Cancel the job before deleting it.")
                with _lock:
                    JOBS.pop(job["id"], None)
                persist_jobs()
            return self.send_json({"ok": True})
        return self.send_error(404)

    def save_config(self, body):
        with _lock:
            cfg = get_config()
            for k in ("autohdr_client_id", "frameio_mode", "frameio_client_id", "frameio_account_id", "mls_limit_kb",
                      "default_indoor_model_id", "default_outdoor_model_id", "frameio_last_folder",
                      "highres_folder_name", "mls_folder_name", "slack_notify", "slack_channel"):
                if k in body:
                    cfg[k] = body[k]
            save_json(CONFIG_PATH, cfg)
        for k in ("autohdr_client_secret", "frameio_client_secret", "slack_webhook_url", "slack_bot_token"):
            if body.get(k):
                kc_set(k, body[k].strip())
        if body.get("slack_clear"):
            kc_delete("slack_webhook_url")
            kc_delete("slack_bot_token")
        if body.get("frameio_disconnect"):
            kc_delete("frameio_access_token")
            kc_delete("frameio_refresh_token")
        return self.send_json(public_config())

    def frameio_connect(self):
        cfg = get_config()
        if not cfg.get("frameio_client_id"):
            raise ApiError("Add the Frame.io client ID in Settings first.")
        state = secrets.token_urlsafe(16)
        with _lock:
            cfg["frameio_oauth_state"] = state
            save_json(CONFIG_PATH, cfg)
        params = {"client_id": cfg["frameio_client_id"], "redirect_uri": REDIRECT_URI,
                  "scope": FRAMEIO_USER_SCOPE, "response_type": "code", "state": state}
        return self.redirect(IMS_AUTHORIZE_URL + "?" + urllib.parse.urlencode(params))

    def frameio_callback(self, q):
        exchange_frameio_code(q)
        return self.redirect("/?frameio=connected#settings")

    def create_job(self, body):
        src = body.get("source") or ""
        info = scan_folder(src)
        if not info["count"]:
            raise ApiError("That folder has no photos in it.")
        name = (body.get("name") or "").strip() or info["name"]
        o = body.get("options") or {}
        fio = body.get("frameio") or {}
        if o.get("do_frameio") and not fio.get("folder_id"):
            raise ApiError("Pick a Frame.io output folder, or turn off the Frame.io upload.")
        if o.get("do_autohdr") and o.get("kind", "raw") == "raw" and not (o.get("indoor_model_id") or o.get("outdoor_model_id")):
            o["note"] = "Using the AutoHDR account default looks."
        if not (o.get("do_autohdr") or o.get("do_resize") or o.get("do_frameio")):
            raise ApiError("Turn on at least one stage: AutoHDR edit, MLS resize, or Frame.io upload.")
        job = {
            "id": uuid.uuid4().hex[:10], "name": name, "source": info["path"], "files": [f["name"] for f in info["files"]],
            "options": o, "frameio": fio, "created": time.time(), "updated": time.time(), "status": "queued",
            "step": None, "step_index": 0, "progress": {"current": 0, "total": 0, "label": ""}, "log": [], "results": {}, "error": None,
        }
        job["steps"] = plan_steps(job)
        with _lock:
            JOBS[job["id"]] = job
            cfg = get_config()
            if fio.get("folder_id"):
                cfg["frameio_last_folder"] = fio
            if o.get("indoor_model_id"):
                cfg["default_indoor_model_id"] = o["indoor_model_id"]
            if o.get("outdoor_model_id"):
                cfg["default_outdoor_model_id"] = o["outdoor_model_id"]
            save_json(CONFIG_PATH, cfg)
        persist_jobs()
        jlog(job, "Queued: %d files from %s" % (len(job["files"]), job["source"]))
        start_job(job)
        return self.send_json(job)


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for j in load_json(JOBS_PATH, {}).get("jobs", []):
        if j.get("status") == "running":
            j["status"] = "interrupted"
            j["log"].append("%s  Server restarted while this job was running. Press Resume." % datetime.now().strftime("%H:%M:%S"))
        JOBS[j["id"]] = j
    persist_jobs()
    start_callback_server()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    log("MLS Studio %s on http://localhost:%d  (data: %s)" % ("DRY RUN" if DRY_RUN else "ready", PORT, DATA_DIR))
    if "--open" in sys.argv:
        subprocess.Popen(["open", "http://localhost:%d" % PORT])
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
