#!/usr/bin/env python3
"""
Instagram Analytics collector for the Studio Hub (instagram-analytics.html).

    python3 scripts/sync_ig_analytics.py                 # normal weekly run
    python3 scripts/sync_ig_analytics.py --out /tmp/ig   # dry run: write JSON files, touch nothing
    python3 scripts/sync_ig_analytics.py --days 120      # first run / backfill a longer window
    python3 scripts/sync_ig_analytics.py --import-adpicks  # seed history from the ad-picks engine
    python3 scripts/sync_ig_analytics.py --check         # say what it can reach, fetch nothing

What it does
  For every client Instagram account (hub/socialAccounts, or --clients FILE) it
  collects each post published in the last --days days (default 35) with its
  views, likes, comments, shares, saves and reach, merges them into a local
  history, and writes:

      hub/igAnalytics       index: accounts + their status, months available
      hub/igPosts_YYYY-MM   one document per month a post was published

Two sources, tried in this order per account
  1. Meta's official API  — the ONLY source of share counts (and of views on
     photos and carousels). Needs a token in
     ~/Library/Application Support/anomaly-social/meta.json, installed with
     scripts/set_meta_token.py (setup guide: Anomaly Creative/Instagram
     Analytics/META-SETUP.md). Without that file this source is skipped and
     the account is reported as "not connected".
  2. Instagram's public page route — views on reels, likes, comments; no
     shares. Instagram has been refusing this route outright ("login
     required") since late August 2026, so expect "blocked" until it relents.

Counts are lifetime totals at collection time. A post keeps being re-sampled
for --days days after it goes up, then its numbers freeze.

Never writes to clients/* and never prints a token or the sync key.
Stdlib only, so launchd can run it with /usr/bin/python3 directly.
"""
import argparse, json, os, random, re, sys, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime

FIRESTORE = "https://firestore.googleapis.com/v1/projects/anomaly-post-pipeline/databases/(default)/documents"
AUTOMATION_ID = "ig-analytics-weekly"           # must equal the SEEDS id in automations.html
STATE_DIR = os.path.expanduser("~/Library/Application Support/anomaly-social")
KEY_PATH = os.path.join(STATE_DIR, "synckey")    # shared with sync_social_stats.py
META_PATH = os.path.join(STATE_DIR, "meta.json")
HISTORY_PATH = os.path.join(STATE_DIR, "ig_history.json")
ADPICKS_STORE = "/Users/danielpan/Desktop/Claude/Next Level Physio : Dr. Jerry/data/store.json"

UA = "AnomalyIgAnalytics/1.0"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
IG_APP_ID = "936619743392459"                    # the id Instagram's own web app sends
IG_BASE = "https://www.instagram.com"
IG_DELAY = (6.0, 10.0)
IG_MAX_PAGES = 4

GRAPH_DEFAULT_HOST = "https://graph.facebook.com"
GRAPH_DEFAULT_VERSION = "v23.0"
GRAPH_RATE_CODES = {4, 17, 32, 613, 80001, 80002}
GRAPH_METRICS = ["views", "reach", "saved", "shares", "likes", "comments", "total_interactions"]
GRAPH_MEDIA_FIELDS = "id,caption,media_type,media_product_type,permalink,timestamp,like_count,comments_count"
GRAPH_INSIGHT_DELAY = 0.2

KEEP_MONTHS = 13
CAP_LEN = 90
DOC_HARD_LIMIT = 1_000_000


class IgBlocked(Exception):
    pass


class AdapterError(Exception):
    pass


class GraphError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def scrub(s):
    """Anything that might echo a request URL gets its token removed first."""
    return re.sub(r"(access_token=)[^&\s\"']+", r"\1***", str(s))


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


def load_accounts(args):
    """[{abbr, handle}] from --clients or the public mirror doc the pipeline keeps."""
    if args.clients:
        with open(args.clients) as fh:
            raw = json.load(fh)
        lst = raw.get("list", raw) if isinstance(raw, dict) else raw
    else:
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
        out.append({"abbr": abbr.upper() if len(abbr) <= 6 and " " not in abbr else abbr, "handle": handle})
    only = {x.strip().upper() for x in (args.only or "").split(",") if x.strip()}
    if only:
        out = [a for a in out if a["abbr"].upper() in only]
    return out


# ---- the post record the page reads --------------------------------------------
def make_post(**kw):
    p = {"id": "", "abbr": "", "h": "", "t": "r", "url": "", "cap": "", "ts": 0,
         "v": None, "l": None, "c": None, "s": None, "sv": None, "rc": None, "src": "p", "at": 0}
    p.update(kw)
    return p


def first_line(text):
    s = (text or "").strip().split("\n")[0].strip()
    return s if len(s) <= CAP_LEN else s[:CAP_LEN - 1].rstrip() + "…"


def shortcode_of(permalink):
    parts = [x for x in urllib.parse.urlparse(permalink or "").path.split("/") if x]
    return parts[-1] if parts else ""


def as_int(v):
    try:
        return int(v) if v is not None and int(v) >= 0 else None
    except (TypeError, ValueError):
        return None


# ---- source 1: Meta's official API ----------------------------------------------
class Graph:
    def __init__(self, host, version, token):
        self.host = host.rstrip("/")
        self.base = f"{self.host}/{version}"
        self.token = token
        self.ig_login = "graph.instagram.com" in self.host

    def get(self, path_or_url, params=None):
        if path_or_url.startswith("http"):
            url, q = path_or_url, dict(params or {})           # paging URLs already carry the token
        else:
            url, q = f"{self.base}/{path_or_url.lstrip('/')}", dict(params or {})
            q["access_token"] = self.token
        if q:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(q)
        err = {}
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.loads(r.read().decode() or "{}")
            except urllib.error.HTTPError as e:
                try:
                    err = (json.loads(e.read().decode() or "{}").get("error")) or {}
                except ValueError:
                    err = {}
                code = err.get("code")
                if code in GRAPH_RATE_CODES or e.code >= 500:
                    wait = 60 * (attempt + 1)
                    print(f"  Meta API throttled (code {code}) — waiting {wait}s")
                    time.sleep(wait)
                    continue
                raise GraphError(scrub(err.get("message") or f"HTTP {e.code}"), code)
            except urllib.error.URLError as e:
                raise GraphError(scrub(f"network error: {e.reason}"))
        raise GraphError(scrub(f"gave up after retries: {err.get('message')}"), err.get("code"))

    def visible_accounts(self):
        """{username: ig_user_id} this token can read."""
        if self.ig_login:
            me = self.get("me", {"fields": "user_id,username"})
            return {str(me.get("username") or "").lower(): str(me.get("user_id") or me.get("id"))}
        found, url, params = {}, "me/accounts", {"fields": "name,instagram_business_account{id,username}", "limit": 100}
        while url:
            d = self.get(url, params)
            params = None
            for page in d.get("data", []):
                iba = page.get("instagram_business_account") or {}
                if iba.get("username") and iba.get("id"):
                    found[iba["username"].lower()] = str(iba["id"])
            url = (d.get("paging") or {}).get("next")
        return found

    def followers(self, ig_id):
        try:
            return as_int(self.get(ig_id, {"fields": "followers_count"}).get("followers_count"))
        except GraphError:
            return None

    def insights(self, media_id):
        metrics, out = list(GRAPH_METRICS), {}
        for attempt in range(3):
            try:
                d = self.get(f"{media_id}/insights", {"metric": ",".join(metrics)})
            except GraphError as e:
                # the API names the metrics this media type allows; ask again for just those
                m = re.search(r"must be one of the following values:\s*(.+)", str(e))
                allowed = [x.strip().strip(".") for x in m.group(1).split(",")] if m else []
                metrics = [x for x in metrics if x in allowed]
                if metrics and attempt < 2:
                    continue
                return {}
            for row in d.get("data", []):
                vals = row.get("values") or []
                out[row.get("name")] = vals[0].get("value") if vals else (row.get("total_value") or {}).get("value")
            break
        return out

    def posts(self, ig_id, acc, since_epoch, now_ms):
        posts, url, params, stop = [], f"{ig_id}/media", {"fields": GRAPH_MEDIA_FIELDS, "limit": 50}, False
        while url and not stop:
            d = self.get(url, params)
            params = None
            for m in d.get("data", []):
                try:
                    ts = int(datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z").timestamp())
                except (KeyError, ValueError):
                    continue
                if ts < since_epoch:
                    stop = True
                    break
                code = shortcode_of(m.get("permalink"))
                if not code:
                    continue
                mt, reel = m.get("media_type") or "IMAGE", m.get("media_product_type") == "REELS"
                ins = self.insights(m["id"])
                time.sleep(GRAPH_INSIGHT_DELAY)
                likes = as_int(ins.get("likes"))
                comments = as_int(ins.get("comments"))
                posts.append(make_post(
                    id=code, abbr=acc["abbr"], h=acc["handle"],
                    t="r" if (reel or mt == "VIDEO") else "c" if mt == "CAROUSEL_ALBUM" else "i",
                    url=m.get("permalink") or "", cap=first_line(m.get("caption")), ts=ts * 1000,
                    v=as_int(ins.get("views")), l=likes if likes is not None else as_int(m.get("like_count")),
                    c=comments if comments is not None else as_int(m.get("comments_count")),
                    s=as_int(ins.get("shares")), sv=as_int(ins.get("saved")), rc=as_int(ins.get("reach")),
                    src="g", at=now_ms))
            url = (d.get("paging") or {}).get("next")
        return posts


def load_graphs():
    """meta.json -> [Graph]. Shapes accepted:
         {"token": "...", "host": "...", "version": "..."}            one token (system user)
         {"tokens": [{"token": "...", "host": "..."} , ...]}           several (Instagram-Login, one per account)"""
    try:
        with open(META_PATH) as fh:
            cfg = json.load(fh)
    except OSError:
        return []
    except ValueError:
        print("meta.json is not valid JSON — ignoring it.")
        return []
    version = cfg.get("version") or GRAPH_DEFAULT_VERSION
    entries = cfg.get("tokens") or ([cfg] if cfg.get("token") else [])
    return [Graph(e.get("host") or cfg.get("host") or GRAPH_DEFAULT_HOST, e.get("version") or version, e["token"])
            for e in entries if e.get("token")]


# ---- source 2: the public page route --------------------------------------------
def ig_get_json(url, params):
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers={
        "User-Agent": BROWSER_UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
        "x-ig-app-id": IG_APP_ID, "Referer": IG_BASE + "/"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            if "json" not in r.headers.get("content-type", ""):
                raise IgBlocked("non-JSON reply")
            return json.loads(r.read().decode("utf8", "ignore"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise AdapterError("account not found")
        raise IgBlocked(f"HTTP {e.code}")
    except urllib.error.URLError as e:
        raise AdapterError(f"network error: {e.reason}")


def public_posts(acc, since_epoch, now_ms):
    posts, max_id, pages = [], None, 0
    while pages < IG_MAX_PAGES:
        pages += 1
        params = {"count": 12}
        if max_id:
            params["max_id"] = max_id
        d = ig_get_json(f"{IG_BASE}/api/v1/feed/user/{acc['handle']}/username/", params)
        items = d.get("items") or []
        older = False
        for it in items:
            code, ts = it.get("code"), int(it.get("taken_at") or 0)
            if not code:
                continue
            if ts < since_epoch:
                if not it.get("pinned_for_users"):
                    older = True
                continue
            video, reel = it.get("media_type") == 2, (it.get("product_type") or "") == "clips"
            views = None
            if video:
                for k in ("play_count", "ig_play_count", "view_count"):
                    if isinstance(it.get(k), int) and it[k] >= 0:
                        views = it[k]
                        break
            posts.append(make_post(
                id=code, abbr=acc["abbr"], h=acc["handle"],
                t="r" if video else "c" if it.get("media_type") == 8 else "i",
                url=f"{IG_BASE}/{'reel' if reel else 'p'}/{code}/",
                cap=first_line((it.get("caption") or {}).get("text")), ts=ts * 1000,
                v=views, l=as_int(it.get("like_count")), c=as_int(it.get("comment_count")),
                s=as_int(it.get("reshare_count")),          # present on some reels; usually absent
                src="p", at=now_ms))
        if older or not (d.get("more_available") and d.get("next_max_id")):
            break
        max_id = d["next_max_id"]
        time.sleep(random.uniform(*IG_DELAY))
    return posts


# ---- history --------------------------------------------------------------------
def history_load():
    try:
        with open(HISTORY_PATH) as fh:
            h = json.load(fh)
            if isinstance(h.get("posts"), dict):
                return h
    except (OSError, ValueError):
        pass
    return {"v": 1, "posts": {}}


def history_save(h):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = HISTORY_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(h, fh)
    os.replace(tmp, HISTORY_PATH)


def merge(history, fresh):
    """Newest sample wins per metric, but a blank never erases a number, and an
    official-API record is never downgraded by a public-page one."""
    n_new = n_upd = 0
    for p in fresh:
        old = history["posts"].get(p["id"])
        if not old:
            history["posts"][p["id"]] = p
            n_new += 1
            continue
        if old.get("src") == "g" and p.get("src") != "g":
            for k in ("l", "c"):                       # likes/comments are public either way
                if p.get(k) is not None:
                    old[k] = p[k]
            old["at"] = p["at"]
        else:
            for k, v in p.items():
                if v is not None and v != "":
                    old[k] = v
        n_upd += 1
    return n_new, n_upd


def import_adpicks(history, accounts, now_ms):
    by_handle = {a["handle"]: a for a in accounts}
    try:
        with open(ADPICKS_STORE) as fh:
            store = json.load(fh)
    except (OSError, ValueError) as e:
        print(f"Couldn't read the ad-picks store ({e}). Skipping import.")
        return 0
    fresh = []
    for p in (store.get("posts") or {}).values():
        acc = by_handle.get(str(p.get("account") or "").lower())
        code = p.get("shortcode") or shortcode_of(p.get("permalink"))
        if not acc or not code or not p.get("epoch"):
            continue
        m, mt = p.get("metrics") or {}, p.get("media_type")
        seen = p.get("last_seen")
        try:
            at = int(datetime.strptime(seen, "%Y-%m-%d").timestamp() * 1000) if seen else now_ms
        except ValueError:
            at = now_ms
        fresh.append(make_post(
            id=code, abbr=acc["abbr"], h=acc["handle"],
            t="r" if (p.get("product_type") == "REELS" or mt == "VIDEO") else "c" if mt == "CAROUSEL_ALBUM" else "i",
            url=p.get("permalink") or "", cap=first_line(p.get("caption")), ts=int(p["epoch"]) * 1000,
            v=as_int(m.get("views")), l=as_int(m.get("likes")), c=as_int(m.get("comments")),
            s=as_int(m.get("shares")), sv=as_int(m.get("saves")), rc=as_int(m.get("reach")),
            src="g" if p.get("source") == "graph" else "p", at=at))
    # imported rows are older samples: only fill posts the history doesn't have yet
    added = 0
    for p in fresh:
        if p["id"] not in history["posts"]:
            history["posts"][p["id"]] = p
            added += 1
    return added


# ---- output ---------------------------------------------------------------------
def month_of(ts_ms):
    return time.strftime("%Y-%m", time.localtime(ts_ms / 1000))


def build_docs(history, accounts_report, warnings, now_ms, days):
    by_month = {}
    for p in history["posts"].values():
        by_month.setdefault(month_of(p["ts"]), []).append(p)
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
    index = {"v": 1, "generatedAt": now_ms, "tz": time.strftime("%Z"), "lookbackDays": days, "months": months,
             "accounts": accounts_report, "warnings": warnings,
             "totals": {"posts": sum(len(by_month[m]) for m in months)}}
    docs["igAnalytics"] = json.dumps(index, separators=(",", ":"), ensure_ascii=False)
    return docs, months


def write(docs, args, key, touched_months):
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
    # month docs first, index last: the page only lists months the index names
    names = [n for n in docs if n != "igAnalytics" and (args.all_months or n[len("igPosts_"):] in touched_months)]
    for name in names + ["igAnalytics"]:
        body = {"fields": {"syncKey": {"stringValue": key}, "at": {"integerValue": at},
                           "data": {"stringValue": docs[name]}}}
        try:
            http_json(f"{FIRESTORE}/hub/{name}", method="PATCH", body=body)
        except urllib.error.HTTPError as e:
            if e.code == 403:
                print(f"Firestore write refused (403) for hub/{name} — the Instagram Analytics rule "
                      f"isn't published yet. Stopping quietly; the local history is kept.")
                return False
            raise
    print(f"Synced hub/igAnalytics + {len(names)} month document(s).")
    return True


# ---- main -----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Collect per-post Instagram numbers for the hub's analytics page.")
    ap.add_argument("--out", help="dry run: write the documents as JSON files into this folder")
    ap.add_argument("--clients", help="JSON list of {abbr|client, ig} instead of hub/socialAccounts")
    ap.add_argument("--days", type=int, default=35, help="how far back to (re)collect posts")
    ap.add_argument("--only", help="comma-separated client codes")
    ap.add_argument("--no-public", action="store_true", help="skip the public page route")
    ap.add_argument("--import-adpicks", action="store_true", help="seed history from the ad-picks engine's store")
    ap.add_argument("--all-months", action="store_true", help="rewrite every month document, not just the ones touched")
    ap.add_argument("--check", action="store_true", help="report what is reachable, fetch no posts")
    args = ap.parse_args()

    if not args.out and not args.check and not preflight():
        return 0
    accounts = load_accounts(args)
    if not accounts:
        sys.exit("No Instagram accounts to collect. Nothing written.")
    key = load_sync_key()
    now_ms = int(time.time() * 1000)
    since = int(time.time()) - args.days * 86400
    warnings, report, fresh = [], {}, []
    for a in accounts:
        report[a["handle"]] = {"abbr": a["abbr"], "handle": a["handle"], "source": "", "status": "pending",
                               "note": "", "followers": None, "posts": 0}

    # 1 — Meta's official API
    graphs = load_graphs()
    ig_ids = {}
    if not graphs:
        print("Meta API: no meta.json — share counts unavailable; every account falls back to the public route.")
    for g in graphs:
        try:
            vis = g.visible_accounts()
        except GraphError as e:
            warnings.append(f"Meta token rejected: {e}")
            print(f"Meta API: token rejected — {e}")
            continue
        for uname, ig_id in vis.items():
            ig_ids.setdefault(uname, (g, ig_id))
    if graphs:
        hit = [a["handle"] for a in accounts if a["handle"] in ig_ids]
        print(f"Meta API: token(s) can read {len(ig_ids)} Instagram account(s); {len(hit)} of {len(accounts)} clients connected.")
    if args.check:
        for a in accounts:
            print(f"  {a['abbr']:>8}  @{a['handle']:<34} {'Meta' if a['handle'] in ig_ids else 'not connected'}")
        return 0
    for a in accounts:
        if a["handle"] not in ig_ids:
            continue
        g, ig_id = ig_ids[a["handle"]]
        r = report[a["handle"]]
        try:
            posts = g.posts(ig_id, a, since, now_ms)
            r.update(source="graph", status="ok", posts=len(posts), followers=g.followers(ig_id))
            fresh += posts
            print(f"  {a['abbr']:>8}  @{a['handle']}: {len(posts)} posts via Meta")
        except GraphError as e:
            r.update(source="graph", status="error", note=str(e)[:140])
            warnings.append(f"{a['abbr']}: Meta API error — {str(e)[:140]}")
            print(f"  {a['abbr']:>8}  @{a['handle']}: Meta API error — {e}")

    # 2 — public page route for whatever Meta didn't cover
    rest = [a for a in accounts if report[a["handle"]]["status"] != "ok"]
    blocked = False
    for i, a in enumerate(rest):
        r = report[a["handle"]]
        if args.no_public or blocked:
            if r["status"] == "pending":
                r.update(source="public" if blocked else "", status="blocked" if blocked else "no-access",
                         note="Instagram is refusing the public route" if blocked else "not connected through Meta")
            continue
        try:
            posts = public_posts(a, since, now_ms)
            r.update(source="public", status="ok", posts=len(posts), note="")
            fresh += posts
            print(f"  {a['abbr']:>8}  @{a['handle']}: {len(posts)} posts via the public route")
        except IgBlocked as e:
            blocked = True
            r.update(source="public", status="blocked", note=f"Instagram refused the public route ({e})")
            print(f"  {a['abbr']:>8}  @{a['handle']}: blocked ({e}) — skipping the public route for the rest of this run")
        except AdapterError as e:
            r.update(source="public", status="error", note=str(e))
            print(f"  {a['abbr']:>8}  @{a['handle']}: {e}")
        if not blocked and i < len(rest) - 1:
            time.sleep(random.uniform(*IG_DELAY))
    if blocked:
        warnings.append("Instagram refused the public route (login required). Accounts not connected through Meta were skipped.")

    # 3 — merge, then publish
    history = history_load()
    imported = import_adpicks(history, accounts, now_ms) if args.import_adpicks else 0
    n_new, n_upd = merge(history, fresh)
    touched = {month_of(p["ts"]) for p in fresh}
    if imported:
        touched = {month_of(p["ts"]) for p in history["posts"].values()}
        print(f"Imported {imported} post(s) from the ad-picks store.")
    have_abbrs = {p["abbr"] for p in history["posts"].values()}
    for r in report.values():
        if r["status"] != "ok" and r["abbr"] in have_abbrs and not r["note"].endswith("(older data kept)"):
            r["note"] = (r["note"] + " (older data kept)").strip()
    print(f"{len(fresh)} post(s) collected — {n_new} new, {n_upd} refreshed; history holds {len(history['posts'])}.")
    if not history["posts"]:
        print("Nothing collected and no history yet. Nothing written.")
        return 0
    docs, months = build_docs(history, sorted(report.values(), key=lambda r: r["abbr"]), warnings, now_ms, args.days)
    if not args.out:
        history_save(history)
    write(docs, args, key, touched)
    return 0


if __name__ == "__main__":
    sys.exit(main())
