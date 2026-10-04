#!/usr/bin/env python3
"""
Instagram Analytics collector for the Studio Hub (instagram-analytics.html).

    python3 scripts/sync_ig_analytics.py                 # normal daily run
    python3 scripts/sync_ig_analytics.py --out /tmp/ig   # dry run: write JSON files, touch nothing
    python3 scripts/sync_ig_analytics.py --check         # which accounts are readable; fetch no posts
    python3 scripts/sync_ig_analytics.py --offline       # ask Instagram nothing; re-upload the local history
    python3 scripts/sync_ig_analytics.py --add URL ...   # also track these older posts (backfill)
    python3 scripts/sync_ig_analytics.py --import-adpicks  # seed history from the ad-picks engine

What it does
  For every client Instagram account in the accounts sheet it reads what
  Instagram shows anyone who is not signed in: views (reels and video), likes
  and comments per post, plus the follower count. Posts are merged into a local
  history and written to:

      hub/igAnalytics       index: accounts + their status, months available
      hub/igPosts_YYYY-MM   one document per month a post was published

Source: Instagram's public embed pages. No login, no token, no Meta app.
  1. instagram.com/<handle>/embed/         the account's 6 newest posts with
                                           likes, comments, date and caption
  2. instagram.com/p/<code>/embed/...      one post: the view count on video,
                                           and fresh numbers for posts that
                                           have dropped out of the newest 6

  Nothing here needs a client to connect anything through Meta Business Suite,
  so nothing here depends on it: shares, saves and reach are private to the
  account owner and are not collected. Views do not exist on photos and
  carousels. An account that is private, has embedding turned off, or was
  renamed shows as "no public profile".

Why it runs every day
  Only the newest 6 posts of an account can be discovered. A daily run sees
  every post of an account that posts up to 6 times a day; the run warns when
  an account's whole window was new, which means some may have been missed.

Counts are lifetime totals at collection time. A post is re-sampled daily for
its first week, every 3 days to three weeks, weekly to --days (35), then its
numbers freeze.

Never writes to clients/* and never prints the sync key.
Stdlib only, so launchd can run it with /usr/bin/python3 directly.
"""
import argparse, csv, hashlib, io, json, os, random, re, sys, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime

FIRESTORE = "https://firestore.googleapis.com/v1/projects/anomaly-post-pipeline/databases/(default)/documents"
AUTOMATION_ID = "ig-analytics-daily"            # must equal the SEEDS id in automations.html
STATE_DIR = os.path.expanduser("~/Library/Application Support/anomaly-social")
KEY_PATH = os.path.join(STATE_DIR, "synckey")    # shared with sync_social_stats.py
HISTORY_PATH = os.path.join(STATE_DIR, "ig_history.json")
SHEET_PATH = os.path.join(STATE_DIR, "accounts_sheet")   # Google Sheet id or link: the account list Daniel maintains
ADPICKS_STORE = "/Users/danielpan/Desktop/Claude/Next Level Physio : Dr. Jerry/data/store.json"

UA = "AnomalyIgAnalytics/2.0"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.5 Safari/605.1.15")
IG_BASE = "https://www.instagram.com"
IG_DELAY = (3.0, 6.0)                            # between requests; this is a guest reading public pages, slowly
IG_EPOCH_MS = 1314220021721                      # post ids carry their own creation time, counted from here
IG_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
CTX_RE = re.compile(r'"contextJSON":"((?:[^"\\]|\\.)*)"')

KEEP_MONTHS = 13
CAP_LEN = 90
DOC_HARD_LIMIT = 1_000_000
FOLLOWER_DAYS = 400                              # daily follower counts kept per account


class IgBlocked(Exception):
    pass


class AdapterError(Exception):
    pass


class SheetError(Exception):
    pass


def http_json(url, method="GET", body=None, headers=None, timeout=60):
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("User-Agent", UA)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


# ---- hub contracts -------------------------------------------------------------
def preflight():
    """hub/automations: paused -> skip the run; note -> surface it."""
    try:
        doc = http_json(FIRESTORE + "/hub/automations")
        lst = json.loads(doc["fields"]["list"]["stringValue"])
        entry = next((a for a in lst if a.get("id") == AUTOMATION_ID), None)
        if entry and entry.get("status") == "paused":
            print("Automation is paused in the hub — skipping run.")
            return False
        if entry and entry.get("note"):
            print("Note from Daniel:", entry["note"])
    except Exception:
        pass
    return True


def load_sync_key():
    try:
        with open(KEY_PATH) as fh:
            return fh.read().strip()
    except OSError:
        return os.environ.get("SOCIAL_SYNC_KEY", "")


def handle_of(raw):
    s = str(raw or "").strip()
    if re.fullmatch(r"@?[A-Za-z0-9._]+", s):
        return s.lstrip("@").lower()
    try:
        u = urllib.parse.urlparse(s if s.startswith("http") else "https://" + s)
    except ValueError:
        return None
    parts = [p for p in u.path.split("/") if p]
    return parts[0].lstrip("@").lower() if parts else None


def sheet_id():
    try:
        with open(SHEET_PATH) as fh:
            raw = fh.read().strip()
    except OSError:
        return ""
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})", raw)
    if m:
        return m.group(1)
    return raw if re.fullmatch(r"[A-Za-z0-9_-]{20,}", raw) else ""


def code_from_name(name, taken):
    """A short stand-in code when the sheet's Code cell is blank: initials, kept unique."""
    words = re.findall(r"[A-Za-z]+", name)
    base = (words[0][:4] if len(words) == 1 else "".join(w[0] for w in words)[:4] or "ACCT").upper()
    code, n = base, 2
    while code in taken:
        code, n = f"{base}{n}", n + 1
    return code


def load_sheet_rows():
    """Rows of the accounts sheet (first tab), or None when no sheet is configured.
    Columns are found by header name: Client, Code, Instagram, TikTok, YouTube, Active.
    The sheet must be shared 'anyone with the link can view' — this job cannot sign in."""
    sid = sheet_id()
    if not sid:
        return None
    req = urllib.request.Request(f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv",
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            ctype = r.headers.get("content-type", "")
            text = r.read().decode("utf-8-sig", "replace")
    except urllib.error.HTTPError as e:
        raise SheetError(f"Google answered HTTP {e.code} for the accounts sheet — check it still exists and is shared by link.")
    except urllib.error.URLError as e:
        raise SheetError(f"couldn't reach Google Sheets ({e.reason})")
    if "csv" not in ctype:
        raise SheetError("the accounts sheet isn't shared as 'anyone with the link can view' (Google sent a sign-in page)")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise SheetError("the accounts sheet is empty")
    head = [h.strip().lower() for h in rows[0]]
    def col(*names):
        for n in names:
            if n in head:
                return head.index(n)
        return None
    ci, cc, cg, ct, cy, ca = col("client", "client name", "name"), col("code", "pipeline code", "abbr"), \
        col("instagram", "ig", "instagram url"), col("tiktok", "tik tok"), col("youtube", "yt", "youtube shorts"), col("active")
    if cg is None:
        raise SheetError("the accounts sheet has no 'Instagram' column in its first row")
    cell = lambda row, i: row[i].strip() if i is not None and i < len(row) else ""
    out, taken = [], set()
    for row in rows[1:]:
        client, code = cell(row, ci), cell(row, cc).upper()
        ig, tt, yt = cell(row, cg), cell(row, ct), cell(row, cy)
        if client.startswith("#") or not (ig or tt or yt):
            continue                                   # help lines and blank rows
        active = cell(row, ca).lower() not in ("no", "n", "false", "0", "off", "paused", "inactive")
        if not code:
            code = code_from_name(client or ig, taken)
        taken.add(code)
        out.append({"client": client or code, "code": code, "ig": ig, "tiktok": tt, "yt": yt, "active": active})
    return out


def load_accounts(args):
    """[{abbr, handle, name}] from --clients, else the accounts sheet, else the pipeline's mirror doc."""
    sheet = None
    if args.clients:
        with open(args.clients) as fh:
            raw = json.load(fh)
        lst = raw.get("list", raw) if isinstance(raw, dict) else raw
    else:
        try:
            sheet = load_sheet_rows()
        except SheetError as e:
            sys.exit(f"Couldn't read the accounts sheet: {e}. Nothing written.")
    if sheet is not None:
        lst = [{"abbr": r["code"], "name": r["client"], "ig": r["ig"]} for r in sheet if r["active"] and r["ig"]]
        paused = sum(1 for r in sheet if not r["active"])
        print(f"Accounts sheet: {len(lst)} active Instagram account(s)" + (f", {paused} paused" if paused else "") + ".")
    elif not args.clients:
        try:
            doc = http_json(FIRESTORE + "/hub/socialAccounts")
            lst = json.loads(doc["fields"]["list"]["stringValue"])
        except urllib.error.HTTPError as e:
            sys.exit(f"Couldn't read hub/socialAccounts ({e.code}). Publish its read rule and enter the "
                     f"client handles in pipeline Settings, or pass --clients <file>. Nothing written.")
        except (KeyError, ValueError):
            sys.exit("hub/socialAccounts exists but has no account list yet. Nothing written.")
    out, seen = [], set()
    for c in lst:
        abbr = str(c.get("abbr") or c.get("client") or "").strip()
        handle = handle_of(c.get("ig"))
        if not abbr or not handle or handle in seen:
            continue
        seen.add(handle)
        out.append({"abbr": abbr.upper() if len(abbr) <= 6 and " " not in abbr else abbr, "handle": handle,
                    "name": str(c.get("name") or "").strip()})
    only = {x.strip().upper() for x in (args.only or "").split(",") if x.strip()}
    if only:
        out = [a for a in out if a["abbr"].upper() in only]
    return out


# ---- the post record the page reads --------------------------------------------
def make_post(**kw):
    """id shortcode, abbr client code, h handle, t r(eel/video)|i(mage)|c(arousel), ts posted (ms),
    v views, l likes, c comments, at last sampled (ms), vat last per-post sample (ms)."""
    p = {"id": "", "abbr": "", "h": "", "t": "r", "url": "", "cap": "", "ts": 0,
         "v": None, "l": None, "c": None, "at": 0}
    p.update(kw)
    return p


def first_line(text):
    s = (text or "").strip().split("\n")[0].strip()
    return s if len(s) <= CAP_LEN else s[:CAP_LEN - 1].rstrip() + "…"


def shortcode_of(permalink):
    parts = [x for x in urllib.parse.urlparse(permalink or "").path.split("/") if x]
    if "embed" in parts:
        parts = parts[:parts.index("embed")]
    return parts[-1] if parts else ""


def shortcode_time_ms(code):
    """When a post was created, read out of its id (the shortcode is that id in base 64)."""
    n = 0
    for ch in code[:11]:
        i = IG_ALPHABET.find(ch)
        if i < 0:
            return 0
        n = n * 64 + i
    return (n >> 23) + IG_EPOCH_MS


def as_int(v):
    try:
        return int(v) if v is not None and int(v) >= 0 else None
    except (TypeError, ValueError):
        return None


def count_of(edge):
    return as_int((edge or {}).get("count")) if isinstance(edge, dict) else None


# ---- the source: Instagram's public embed pages ---------------------------------
def ig_page(path):
    """One public page, as any visitor who is not signed in would get it."""
    req = urllib.request.Request(IG_BASE + path, headers={
        "User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            if "/accounts/login" in r.geturl():
                raise IgBlocked("sent to the login page")
            return r.read().decode("utf8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise AdapterError("not found")
        raise IgBlocked(f"HTTP {e.code}")
    except urllib.error.URLError as e:
        raise AdapterError(f"network error: {e.reason}")


def embed_context(page):
    m = CTX_RE.search(page)
    if not m:
        return None
    try:
        return json.loads(json.loads('"' + m.group(1) + '"'))
    except ValueError:
        return None


def caption_of(sm):
    edges = (sm.get("edge_media_to_caption") or {}).get("edges") or []
    return first_line(((edges[0] or {}).get("node") or {}).get("text")) if edges else ""


def kind_of(sm):
    tn = sm.get("__typename") or ""
    return "r" if (sm.get("is_video") or tn == "GraphVideo") else "c" if tn == "GraphSidecar" else "i"


def profile_posts(acc, now_ms):
    """The account's newest posts (Instagram shows 6) -> (posts, followers, total posts on the account). Likes and
    comments come with the list; views do not, they are read per post afterwards."""
    page = ig_page(f"/{acc['handle']}/embed/")
    ctx = embed_context(page)
    if ctx is None:
        if "EmbedIsBroken" in page or "may be broken" in page:
            raise AdapterError("no public profile (private, embedding turned off, or the handle changed)")
        if "/accounts/login" in page[:20000]:
            raise IgBlocked("login wall")
        raise AdapterError("page layout not recognised")
    c = ctx.get("context") or {}
    posts = []
    for item in c.get("graphql_media") or []:
        sm = (item or {}).get("shortcode_media") or {}
        code = sm.get("shortcode")
        if not code:
            continue
        t = kind_of(sm)
        hidden = bool(sm.get("like_and_view_counts_disabled"))
        ts = int(sm.get("taken_at_timestamp") or 0) * 1000 or shortcode_time_ms(code)
        posts.append(make_post(
            id=code, abbr=acc["abbr"], h=acc["handle"], t=t,
            url=f"{IG_BASE}/{'reel' if t == 'r' else 'p'}/{code}/", cap=caption_of(sm), ts=ts,
            l=None if hidden else count_of(sm.get("edge_liked_by")),
            c=first_int(count_of(sm.get("edge_media_to_comment")), sm.get("commenter_count")), at=now_ms))
    return posts, as_int(c.get("followers_count")), as_int(c.get("posts_count"))


def first_int(*vals):
    for v in vals:
        n = as_int(v)
        if n is not None:
            return n
    return None


def post_numbers(code):
    """One post's public numbers: {v, l, c, t, cap, owner} — or {"gone": True} once it has been deleted."""
    page = ig_page(f"/p/{code}/embed/captioned/")
    ctx = embed_context(page)
    sm = ((ctx or {}).get("gql_data") or {}).get("shortcode_media") or {}
    if sm:
        return {"v": as_int(sm.get("video_view_count")) if sm.get("is_video") else None,
                "l": count_of(sm.get("edge_liked_by")), "c": count_of(sm.get("edge_media_to_comment")),
                "t": kind_of(sm), "cap": caption_of(sm), "owner": ((sm.get("owner") or {}).get("username") or "").lower()}
    if "EmbedIsBroken" in page or "may be broken" in page:
        return {"gone": True}
    # some posts are served as plain HTML without the data block; the numbers are still in the markup
    def num(pat):
        m = re.search(pat, page)
        return as_int(m.group(1).replace(",", "")) if m else None
    out = {"v": num(r'video_view_count\\?":\s*(\d+)'), "l": num(r'edge_liked_by\\?":\{\\?"count\\?":\s*(\d+)') or num(r'([\d,]+) likes?<'),
           "c": num(r'edge_media_to_comment\\?":\{\\?"count\\?":\s*(\d+)')}
    if out["v"] is None and out["l"] is None and out["c"] is None:
        if "/accounts/login" in page[:20000]:
            raise IgBlocked("login wall")
        raise AdapterError("no numbers on the page")
    return out


def due(p, now_ms, days):
    """Is this post's per-post sample due? Daily in week one, every 3 days to three weeks, weekly to --days."""
    age = (now_ms - p["ts"]) / 86400000.0
    if age > days:
        return False
    since = (now_ms - (p.get("vat") or 0)) / 3600000.0
    return since >= (20 if age <= 7 else 68 if age <= 21 else 164)


# ---- history --------------------------------------------------------------------
def history_load():
    try:
        with open(HISTORY_PATH) as fh:
            h = json.load(fh)
            if isinstance(h.get("posts"), dict):
                for p in h["posts"].values():          # fields from the retired Meta route
                    for k in ("s", "sv", "rc", "src"):
                        p.pop(k, None)
                h.setdefault("followers", {})
                h.setdefault("cover", {})
                h.setdefault("status", {})
                return h
    except (OSError, ValueError):
        pass
    return {"v": 2, "posts": {}, "followers": {}, "cover": {}, "status": {}}


def history_save(h):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = HISTORY_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(h, fh)
    os.replace(tmp, HISTORY_PATH)


def merge(history, fresh):
    """Newest sample wins per field, but a blank never erases a number, and a post keeps the account it was
    first seen on (a collab post shows up on both clients' pages)."""
    n_new = n_upd = 0
    for p in fresh:
        old = history["posts"].get(p["id"])
        if not old:
            history["posts"][p["id"]] = p
            n_new += 1
            continue
        for k, v in p.items():
            if v is not None and v != "" and k not in ("abbr", "h"):
                old[k] = v
        n_upd += 1
    return n_new, n_upd


def note_followers(history, handle, n, now_ms):
    if n is None:
        return
    days = history["followers"].setdefault(handle, {})
    days[time.strftime("%Y-%m-%d", time.localtime(now_ms / 1000))] = n
    for d in sorted(days)[:-FOLLOWER_DAYS]:
        del days[d]


def followers_ago(history, handle, now_ms, days_back, slack=4):
    """The follower count nearest to days_back days ago (within slack days), else None."""
    days = history["followers"].get(handle) or {}
    best = None
    for d, n in days.items():
        try:
            age = (now_ms / 1000 - time.mktime(time.strptime(d, "%Y-%m-%d"))) / 86400
        except ValueError:
            continue
        gap = abs(age - days_back)
        if gap <= slack and (best is None or gap < best[0]):
            best = (gap, n)
    return best[1] if best else None


def import_adpicks(history, accounts, now_ms):
    by_handle = {a["handle"]: a for a in accounts}
    try:
        with open(ADPICKS_STORE) as fh:
            store = json.load(fh)
    except (OSError, ValueError) as e:
        print(f"Couldn't read the ad-picks store ({e}). Skipping import.")
        return 0
    added = 0
    for p in (store.get("posts") or {}).values():
        acc = by_handle.get(str(p.get("account") or "").lower())
        code = p.get("shortcode") or shortcode_of(p.get("permalink"))
        if not acc or not code or not p.get("epoch") or code in history["posts"]:
            continue                                   # imported rows are older samples: only fill what is missing
        m, mt = p.get("metrics") or {}, p.get("media_type")
        seen = p.get("last_seen")
        try:
            at = int(datetime.strptime(seen, "%Y-%m-%d").timestamp() * 1000) if seen else now_ms
        except ValueError:
            at = now_ms
        history["posts"][code] = make_post(
            id=code, abbr=acc["abbr"], h=acc["handle"],
            t="r" if (p.get("product_type") == "REELS" or mt == "VIDEO") else "c" if mt == "CAROUSEL_ALBUM" else "i",
            url=p.get("permalink") or "", cap=first_line(p.get("caption")), ts=int(p["epoch"]) * 1000,
            # the store's "views" is Instagram's play count, a bigger number than the public view count used
            # everywhere else here; leave it blank and the next run reads the public one for this post
            v=None, l=as_int(m.get("likes")), c=as_int(m.get("comments")), at=at)
        added += 1
    return added


# ---- output ---------------------------------------------------------------------
def month_of(ts_ms):
    return time.strftime("%Y-%m", time.localtime(ts_ms / 1000))


def build_docs(history, accounts_report, warnings, now_ms, days):
    by_month = {}
    for p in history["posts"].values():
        if not p.get("gone"):                          # deleted on Instagram: kept locally, not published
            by_month.setdefault(month_of(p["ts"]), []).append({k: v for k, v in p.items() if k != "vat"})
    months = sorted(by_month)[-KEEP_MONTHS:]
    docs = {}
    for m in months:
        posts = sorted(by_month[m], key=lambda p: -p["ts"])
        data = json.dumps({"month": m, "posts": posts}, separators=(",", ":"), ensure_ascii=False)
        if len(data.encode()) > DOC_HARD_LIMIT - 50_000:
            for p in posts:
                p["cap"] = p["cap"][:40]
            data = json.dumps({"month": m, "posts": posts}, separators=(",", ":"), ensure_ascii=False)
        assert len(data.encode()) < DOC_HARD_LIMIT, f"{m} would exceed the 1 MiB Firestore document cap"
        docs["igPosts_" + m] = data
    index = {"v": 2, "source": "public", "generatedAt": now_ms, "tz": time.strftime("%Z"), "lookbackDays": days, "months": months,
             "accounts": accounts_report, "warnings": warnings,
             "totals": {"posts": sum(len(by_month[m]) for m in months)}}
    docs["igAnalytics"] = json.dumps(index, separators=(",", ":"), ensure_ascii=False)
    return docs, months


def write(docs, args, key, history):
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        for name, data in docs.items():
            with open(os.path.join(args.out, name + ".json"), "w") as fh:
                fh.write(data)
        print(f"Dry run: wrote {len(docs)} file(s) to {args.out} "
              f"({sum(len(d) for d in docs.values()):,} bytes). Nothing sent to the hub.")
        return True
    if not key:
        print("No sync key (expected ~/Library/Application Support/anomaly-social/synckey). Nothing written.")
        return False
    at = str(int(time.time() * 1000))
    # A month document is uploaded when its content differs from what was last
    # confirmed uploaded — so a run that was refused half-way is simply retried
    # next time instead of leaving a hole. Month docs first, index last: the
    # page only fetches months the index names.
    synced = history.setdefault("synced", {})
    digest = {n: hashlib.sha1(d.encode()).hexdigest() for n, d in docs.items()}
    names = [n for n in sorted(docs) if n != "igAnalytics" and (args.all_months or synced.get(n) != digest[n])]
    for name in names + ["igAnalytics"]:
        body = {"fields": {"syncKey": {"stringValue": key}, "at": {"integerValue": at},
                           "data": {"stringValue": docs[name]}}}
        try:
            http_json(f"{FIRESTORE}/hub/{name}", method="PATCH", body=body)
        except urllib.error.HTTPError as e:
            history_save(history)
            if e.code == 403:
                print(f"Firestore write refused (403) for hub/{name} — the Instagram Analytics rule "
                      f"isn't published yet. Stopping quietly; the local history is kept and will upload next run.")
                return False
            raise
        synced[name] = digest[name]
    history_save(history)
    print(f"Synced hub/igAnalytics + {len(names)} month document(s).")
    return True


# ---- main -----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Collect per-post Instagram numbers for the hub's analytics page.")
    ap.add_argument("--out", help="dry run: write the documents as JSON files into this folder")
    ap.add_argument("--clients", help="JSON list of {abbr|client, ig} instead of the accounts sheet")
    ap.add_argument("--days", type=int, default=35, help="how long a post keeps being re-sampled")
    ap.add_argument("--only", help="comma-separated client codes")
    ap.add_argument("--max-fetch", type=int, default=300, help="most per-post pages to read in one run")
    ap.add_argument("--add", nargs="*", default=[], metavar="URL", help="post links to start tracking (older posts the newest-6 list no longer shows)")
    ap.add_argument("--import-adpicks", action="store_true", help="seed history from the ad-picks engine's store")
    ap.add_argument("--all-months", action="store_true", help="re-upload every month document, changed or not")
    ap.add_argument("--check", action="store_true", help="report which accounts are readable; fetch no posts")
    ap.add_argument("--offline", action="store_true", help="ask Instagram nothing: rebuild and upload from the local history")
    args = ap.parse_args()

    if not args.out and not args.check and not preflight():
        return 0
    accounts = load_accounts(args)
    if not accounts:
        sys.exit("No Instagram accounts to collect. Nothing written.")
    key = load_sync_key()
    now_ms = int(time.time() * 1000)
    history = history_load()
    first_run = not history["posts"]
    warnings, report, fresh, listed = [], {}, {}, set()
    by_handle = {a["handle"]: a for a in accounts}
    for a in accounts:
        report[a["handle"]] = {"abbr": a["abbr"], "name": a.get("name") or "", "handle": a["handle"], "source": "public",
                               "status": "pending", "note": "", "followers": None, "f7": None, "f30": None, "posts": 0, "since": None}
    pause = lambda: time.sleep(random.uniform(*IG_DELAY))

    # 1 — each account's newest posts
    blocked = None
    for i, a in enumerate(accounts):
        r = report[a["handle"]]
        if args.offline:
            days = history["followers"].get(a["handle"]) or {}
            last = history["status"].get(a["handle"]) or {}
            n = sum(1 for p in history["posts"].values() if p.get("h") == a["handle"])
            r.update(status=last.get("status") or ("ok" if n else "pending"), posts=n, followers=days[max(days)] if days else None,
                     note=last.get("note") or ("" if n else "not collected yet"))
            continue
        if blocked:
            r.update(status="blocked", note="Instagram stopped answering earlier in this run")
            continue
        try:
            posts, followers, n_total = profile_posts(a, now_ms)
            known = {p["id"] for p in history["posts"].values() if p.get("h") == a["handle"]}
            new = [p for p in posts if p["id"] not in history["posts"]]
            r.update(status="ok", posts=len(posts), followers=followers)
            if known and len(posts) >= 6 and len(new) == len(posts):
                warnings.append(f"{a['abbr']}: every post Instagram listed was new, so some since the last run may have been missed.")
            for p in posts:
                fresh.setdefault(p["id"], p)
                listed.add(p["id"])
            if not args.check:
                note_followers(history, a["handle"], followers, now_ms)
                # From the oldest post in the first listing onwards nothing can be missing, so that is where this
                # account's complete record starts (0 = from its very first post). The page will not compare a
                # period against one that began before it.
                if a["handle"] not in history["cover"] and posts:
                    history["cover"][a["handle"]] = 0 if (n_total is not None and n_total <= len(posts)) else min(p["ts"] for p in posts)
            print(f"  {a['abbr']:>8}  @{a['handle']:<32} {len(posts)} posts ({len(new)} new) · {followers if followers is not None else '?'} followers")
        except IgBlocked as e:
            blocked = str(e)
            r.update(status="blocked", note=f"Instagram refused the page ({e})")
            print(f"  {a['abbr']:>8}  @{a['handle']:<32} blocked ({e}) — stopping requests for this run")
        except AdapterError as e:
            r.update(status="error", note=str(e))
            print(f"  {a['abbr']:>8}  @{a['handle']:<32} {e}")
        if not blocked and i < len(accounts) - 1:
            pause()
    if args.check:
        ok = sum(1 for r in report.values() if r["status"] == "ok")
        print(f"{ok} of {len(accounts)} account(s) readable. Nothing written.")
        return 0

    if not args.offline:
        for r in report.values():
            history["status"][r["handle"]] = {"status": r["status"], "note": r["note"]}

    # 2 — links handed in with --add
    for raw in args.add:
        code = shortcode_of(raw) if "/" in raw else raw.strip()
        if code and code not in fresh and code not in history["posts"]:
            fresh[code] = make_post(id=code, abbr="", h="", t="r", url=f"{IG_BASE}/p/{code}/", ts=shortcode_time_ms(code), at=now_ms)

    # 3 — per-post pages: the view count on video, and fresh numbers for posts no longer among the newest 6
    todo = []
    for pid, p in fresh.items():
        old = history["posts"].get(pid) or {}
        probe = dict(old, **{k: v for k, v in p.items() if v is not None})
        # a video seen for the first time gets one reading even when it is already past the sampling window
        if not p["abbr"] or (p["t"] == "r" and (due(probe, now_ms, args.days) or not probe.get("vat"))):
            todo.append(p)
    for pid, old in history["posts"].items():
        never = old.get("t") == "r" and old.get("v") is None and not old.get("vat")
        if pid not in fresh and not old.get("gone") and (due(old, now_ms, args.days) or never):
            todo.append(dict(old))
    if args.offline:
        todo = []
    todo.sort(key=lambda p: -p["ts"])
    skipped = max(0, len(todo) - args.max_fetch)
    todo = todo[:args.max_fetch]
    print(f"Reading {len(todo)} post page(s) for view counts" + (f" ({skipped} more left for the next run)" if skipped else "") + "…")
    read = 0
    for p in todo:
        if blocked:
            break
        pause()
        try:
            n = post_numbers(p["id"])
        except IgBlocked as e:
            blocked = str(e)
            print(f"  blocked ({e}) after {read} post page(s) — the rest wait for the next run")
            break
        except AdapterError as e:
            print(f"  {p['id']}: {e}")
            continue
        read += 1
        if n.get("gone"):
            p["gone"] = 1
        else:
            if not p["abbr"]:                           # an --add link: file it under the account that owns it
                acc = by_handle.get(n.get("owner") or "")
                if not acc:
                    print(f"  {p['id']}: belongs to @{n.get('owner') or '?'}, which is not in the accounts sheet — skipped")
                    fresh.pop(p["id"], None)
                    continue
                p.update(abbr=acc["abbr"], h=acc["handle"], t=n.get("t") or "r", cap=n.get("cap") or "")
                p["url"] = f"{IG_BASE}/{'reel' if p['t'] == 'r' else 'p'}/{p['id']}/"
            for k in ("v", "l", "c"):
                if n.get(k) is not None:
                    p[k] = n[k]
        p["vat"] = p["at"] = now_ms
        fresh[p["id"]] = p
    for pid in [pid for pid, p in fresh.items() if not p["abbr"]]:
        del fresh[pid]                                  # an --add link that was never resolved
    if blocked:
        warnings.append("Instagram stopped answering part-way through the last run, so some numbers are a day older than the rest.")

    # 4 — merge, then publish
    imported = import_adpicks(history, accounts, now_ms) if args.import_adpicks else 0
    n_new, n_upd = merge(history, list(fresh.values()))
    if imported:
        print(f"Imported {imported} post(s) from the ad-picks store.")
    have = {p["h"] for p in history["posts"].values()}
    for r in report.values():
        r["f7"], r["f30"] = followers_ago(history, r["handle"], now_ms, 7), followers_ago(history, r["handle"], now_ms, 30, slack=6)
        r["since"] = history["cover"].get(r["handle"])
        if r["status"] != "ok" and r["handle"] in have:
            r["note"] = (r["note"] + " (older data kept)").strip()
    print(f"{len(fresh)} post(s) sampled — {n_new} new, {n_upd} refreshed, {read} post page(s) read; history holds {len(history['posts'])}.")
    if first_run and n_new:
        print("First run: Instagram only lists each account's newest 6 posts, so the history starts here and grows daily.")
    if not history["posts"]:
        print("Nothing collected and no history yet. Nothing written.")
        return 0
    docs, months = build_docs(history, sorted(report.values(), key=lambda r: r["abbr"]), warnings, now_ms, args.days)
    if not args.out:
        history_save(history)
    write(docs, args, key, history)
    return 0


if __name__ == "__main__":
    sys.exit(main())
