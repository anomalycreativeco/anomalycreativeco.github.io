#!/usr/bin/env python3
"""Weekly social stats for the Studio Hub.

Collects last week's per-post view counts for every client account listed in
hub/socialAccounts (Instagram, TikTok, YouTube Shorts), ranks them, and writes
one doc, hub/socialStats, that the hub's "Social · last week" widget renders.

Everything is anonymous and stdlib-only, like sync_josh_tracker.py:
  YouTube / TikTok  via the yt-dlp binary (shell out; -J listings)
  Instagram         via Instagram's own public web feed endpoint, ported from
                    the ad-picks engine's public_source.py (username-addressed
                    route, 12 posts/page, browser headers, stop on first block)
  Facebook          not collected — reels shared from IG to FB are already
                    counted in the IG reel's views (Meta counts them together)

"Views" = lifetime views at collection time for posts PUBLISHED in the previous
Sunday->Saturday week, Mac-local time. TikTok listing counts are rounded to
3 significant figures by the site; they are stored as-is with viewsExact:false.

Instagram's anonymous route is IP-budgeted (roughly 6-9 feed pages per ~2h,
shared with the monthly ad-picks job). One page per account is enough for a
week; when a round hits a 401 the rest wait 2h and the doc is written as
mode:"partial" in between, so the widget still updates within minutes.

Run:
  python3 sync_social_stats.py                       # normal run, writes Firestore
  python3 sync_social_stats.py --out /tmp/s.json     # dry run: writes the JSON file instead
  python3 sync_social_stats.py --clients acc.json    # use a local accounts list (same shape as the mirror doc)
  python3 sync_social_stats.py --week 2026-08-30     # re-run a past week (a Sunday)
  --no-ig  --no-thumbs  --ig-rounds N  --update-ytdlp

Sync key lives OUTSIDE the public repo:
  ~/Library/Application Support/anomaly-social/synckey   (or env SOCIAL_SYNC_KEY)
"""
import argparse, base64, json, os, random, re, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

FIRESTORE = "https://firestore.googleapis.com/v1/projects/anomaly-post-pipeline/databases/(default)/documents"
AUTOMATION_ID = "social-stats-weekly"          # must equal the SEEDS id in automations.html
YTDLP = "/Users/danielpan/.local/bin/yt-dlp"
STATE_DIR = os.path.expanduser("~/Library/Application Support/anomaly-social")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
KEY_PATH = os.path.join(STATE_DIR, "synckey")
ADPICKS_LOG = "/Users/danielpan/Desktop/Claude/Next Level Physio : Dr. Jerry/logs/catchup.log"

UA = "AnomalySocialStats/1.0"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
IG_APP_ID = "936619743392459"                  # the id Instagram's own web app sends
IG_BASE = "https://www.instagram.com"
IG_DELAY = (6.0, 10.0)
IG_MAX_PAGES_PER_ACCOUNT = 2
IG_ROUND_WAIT = 2 * 3600
YT_LIST_N, YT_MAX_DRILL, TT_LIST_N, TT_LIST_DEEP = 40, 24, 30, 120
THUMB_W, THUMB_MAX_BYTES = 160, 8_000
DOC_SOFT_LIMIT, DOC_HARD_LIMIT = 700_000, 1_000_000
MAX_POSTS_IN_DOC = 300


# ---- plumbing ---------------------------------------------------------------
def http(url, method="GET", body=None, headers=None, timeout=60):
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("User-Agent", UA)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def preflight():
    """hub/automations contract: paused -> skip the run; note -> surface it."""
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
        pass
    return True


def load_sync_key():
    try:
        with open(KEY_PATH) as fh:
            k = fh.read().strip()
            if k:
                return k
    except OSError:
        pass
    return os.environ.get("SOCIAL_SYNC_KEY", "")


def load_accounts(args):
    """[{abbr, ig?, tiktok?, yt?, fb?}] from --clients or the public mirror doc."""
    if args.clients:
        with open(args.clients) as fh:
            raw = json.load(fh)
        lst = raw.get("list", raw) if isinstance(raw, dict) else raw
    else:
        try:
            doc = http(FIRESTORE + "/hub/socialAccounts")
            lst = json.loads(doc["fields"]["list"]["stringValue"])
        except urllib.error.HTTPError as e:
            sys.exit(f"Couldn't read hub/socialAccounts ({e.code}). Publish its read rule, "
                     f"or pass --clients <file>. Nothing written.")
    out = []
    for c in lst:
        abbr = str(c.get("abbr") or "").strip().upper()
        if not abbr:
            continue
        for plat in ("ig", "tiktok", "yt"):
            if c.get(plat):
                out.append({"abbr": abbr, "client": c.get("name") or "", "platform": plat, "url": c[plat]})
        if c.get("fb"):
            out.append({"abbr": abbr, "client": c.get("name") or "", "platform": "fb", "url": c["fb"]})
    return out


# ---- week + accounts ----------------------------------------------------------
def week_window(args):
    now = datetime.now().astimezone()
    if args.week:
        start = datetime.strptime(args.week, "%Y-%m-%d").replace(tzinfo=now.tzinfo)
        if start.weekday() != 6:
            sys.exit("--week must be a Sunday (YYYY-MM-DD)")
    else:
        this_sunday = (now - timedelta(days=(now.weekday() + 1) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)
        start = this_sunday - timedelta(days=7)
    end_excl = start + timedelta(days=7)
    return {"start": start, "end_excl": end_excl,
            "start_epoch": int(start.timestamp()), "end_epoch": int(end_excl.timestamp()),
            "weekStart": start.strftime("%Y-%m-%d"), "weekEnd": (end_excl - timedelta(days=1)).strftime("%Y-%m-%d"),
            "tz": str(now.tzinfo)}


def handle_of(platform, url):
    """Profile URL -> handle; accepts bare handles too. None if unparseable."""
    s = url.strip()
    if re.fullmatch(r"@?[A-Za-z0-9._-]+", s):
        return s.lstrip("@")
    try:
        u = urllib.parse.urlparse(s if s.startswith("http") else "https://" + s)
    except ValueError:
        return None
    parts = [p for p in u.path.split("/") if p]
    if not parts:
        return None
    if platform == "yt":
        if parts[0] in ("c", "channel", "user") and len(parts) > 1:
            return "/".join(parts[:2])          # kept as a path; listing URL built below
        return parts[0].lstrip("@")
    return parts[0].lstrip("@")


def listing_url(platform, handle):
    if platform == "ig":
        return f"{IG_BASE}/{handle}/"
    if platform == "tiktok":
        return f"https://www.tiktok.com/@{handle}"
    if platform == "yt":
        return f"https://www.youtube.com/{handle}/shorts" if "/" in handle else f"https://www.youtube.com/@{handle}/shorts"
    return None


def post(**kw):
    base = {"platform": "", "abbr": "", "account": "", "id": "", "title": "", "views": None, "viewsExact": True,
            "likes": None, "comments": None, "postedAt": 0, "permalink": "", "thumbUrl": None}
    base.update(kw)
    return base


# ---- yt-dlp adapters ----------------------------------------------------------
class AdapterError(Exception):
    pass


def ytdlp_json(url, extra, timeout=120):
    cmd = [YTDLP, "--ignore-config", "--no-warnings", "-J"] + extra + [url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 or not r.stdout.strip():
        raise AdapterError((r.stderr or "no output").strip()[-400:])
    return json.loads(r.stdout)


def collect_youtube(acc, win):
    handle = handle_of("yt", acc["url"])
    if not handle:
        raise AdapterError("unparseable YouTube link")
    listing = ytdlp_json(listing_url("yt", handle), ["--flat-playlist", "--playlist-end", str(YT_LIST_N)])
    entries = listing.get("entries") or []
    posts, drilled, truncated = [], 0, False
    for e in entries:
        vid = e.get("id")
        if not vid:
            continue
        if drilled >= YT_MAX_DRILL:
            truncated = True
            break
        drilled += 1
        d = ytdlp_json(f"https://www.youtube.com/shorts/{vid}", ["--skip-download"], timeout=90)
        ts = d.get("timestamp")
        if not ts and d.get("upload_date"):
            ts = int(datetime.strptime(d["upload_date"] + "12", "%Y%m%d%H").replace(tzinfo=timezone.utc).timestamp())
        ts = int(ts or 0)
        if ts and ts < win["start_epoch"]:
            break                                   # listings are newest-first
        if win["start_epoch"] <= ts < win["end_epoch"]:
            posts.append(post(platform="yt", abbr=acc["abbr"], account=handle, id=vid, title=(d.get("title") or "")[:80],
                              views=d.get("view_count"), viewsExact=True, likes=d.get("like_count"),
                              comments=d.get("comment_count"), postedAt=ts,
                              permalink=f"https://www.youtube.com/shorts/{vid}",
                              thumbUrl=f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"))
    return posts, {"pages": 1 + drilled, "note": f"truncated at {YT_MAX_DRILL} shorts" if truncated else ""}


def collect_tiktok(acc, win):
    handle = handle_of("tiktok", acc["url"])
    if not handle:
        raise AdapterError("unparseable TikTok link")
    listing = ytdlp_json(listing_url("tiktok", handle), ["--flat-playlist", "--playlist-end", str(TT_LIST_N)])
    entries = listing.get("entries") or []
    # a busy account can post 30+ times after the week closed (a --week re-run
    # weeks back, or a Friday test) — go deeper if nothing listed reaches the week
    oldest = min((int(e.get("timestamp") or 0) for e in entries if e.get("timestamp")), default=0)
    if entries and oldest >= win["start_epoch"] and len(entries) >= TT_LIST_N:
        listing = ytdlp_json(listing_url("tiktok", handle), ["--flat-playlist", "--playlist-end", str(TT_LIST_DEEP)])
        entries = listing.get("entries") or []
    posts = []
    for e in entries:
        ts = int(e.get("timestamp") or 0)
        if not (win["start_epoch"] <= ts < win["end_epoch"]):
            continue                                # pinned videos are older and fall out here
        vid = str(e.get("id") or "")
        thumbs = e.get("thumbnails") or []
        posts.append(post(platform="tiktok", abbr=acc["abbr"], account=handle, id=vid,
                          title=(e.get("title") or e.get("description") or "")[:80],
                          views=e.get("view_count"), viewsExact=False, likes=e.get("like_count"),
                          comments=e.get("comment_count"), postedAt=ts,
                          permalink=e.get("url") or f"https://www.tiktok.com/@{handle}/video/{vid}",
                          thumbUrl=(thumbs[0].get("url") if thumbs else None)))
    oldest = min((int(e.get("timestamp") or 0) for e in entries if e.get("timestamp")), default=0)
    note = "listing didn't reach back past the week — counts may be low" if entries and oldest >= win["start_epoch"] else ""
    return posts, {"pages": 1, "note": note}


# ---- Instagram (stdlib port of the engine's public route) ---------------------
class IgBlocked(Exception):
    pass


def ig_get_json(url, params):
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={
        "User-Agent": BROWSER_UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
        "x-ig-app-id": IG_APP_ID, "Referer": IG_BASE + "/"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            ctype = r.headers.get("content-type", "")
            body = r.read().decode("utf8", "ignore")
            if "json" not in ctype:
                raise IgBlocked("non-json 200")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise AdapterError("account not found")
        raise IgBlocked(f"HTTP {e.code}")


def _ig_thumb(item):
    cands = (item.get("image_versions2") or {}).get("candidates") or []
    if not cands and item.get("carousel_media"):
        cands = (item["carousel_media"][0].get("image_versions2") or {}).get("candidates") or []
    if not cands:
        return None
    small = [c for c in cands if (c.get("width") or 0) >= 300]
    pick = min(small, key=lambda c: c.get("width", 0)) if small else max(cands, key=lambda c: c.get("width", 0))
    return pick.get("url")


def collect_instagram(acc, win):
    handle = handle_of("ig", acc["url"])
    if not handle:
        raise AdapterError("unparseable Instagram link")
    posts, max_id, pages = [], None, 0
    while pages < IG_MAX_PAGES_PER_ACCOUNT:
        pages += 1
        params = {"count": 12}
        if max_id:
            params["max_id"] = max_id
        d = ig_get_json(f"{IG_BASE}/api/v1/feed/user/{handle}/username/", params)
        items = d.get("items") or []
        all_in_window = bool(items)
        for it in items:
            code, pk = it.get("code"), it.get("pk") or it.get("id")
            if not code or not pk:
                continue
            ts = int(it.get("taken_at") or 0)
            if not it.get("pinned_for_users") and ts < win["start_epoch"]:
                all_in_window = False
            if not (win["start_epoch"] <= ts < win["end_epoch"]):
                continue
            is_video = it.get("media_type") == 2
            views = None
            if is_video:
                for k in ("play_count", "ig_play_count", "view_count"):
                    if isinstance(it.get(k), int) and it[k] >= 0:
                        views = it[k]
                        break
            reel = (it.get("product_type") or "") == "clips"
            posts.append(post(platform="ig", abbr=acc["abbr"], account=handle, id=str(pk).split("_")[0],
                              title=(((it.get("caption") or {}).get("text") or "").strip().split("\n")[0])[:80],
                              views=views, viewsExact=True, likes=it.get("like_count"), comments=it.get("comment_count"),
                              postedAt=ts, permalink=f"{IG_BASE}/{'reel' if reel else 'p'}/{code}/",
                              thumbUrl=_ig_thumb(it)))
        if not (all_in_window and d.get("more_available") and d.get("next_max_id")):
            break
        max_id = d["next_max_id"]
        time.sleep(random.uniform(*IG_DELAY))
    return posts, {"pages": pages, "note": ""}


# ---- aggregation --------------------------------------------------------------
def sort_key(p):
    return (-(p["views"] if p["views"] is not None else -1), -int(bool(p["viewsExact"])), -(p["likes"] or 0), -(p["postedAt"] or 0))


def aggregate(posts, accounts, win, prev, mode, warnings, tools):
    by_plat = {}
    for plat in ("ig", "tiktok", "yt"):
        pp = [p for p in posts if p["platform"] == plat]
        accs = [a for a in accounts if a["platform"] == plat]
        statuses = {a["status"] for a in accs}
        status = "none" if not accs else ("ok" if statuses <= {"ok"} else ("error" if "ok" not in statuses else "partial"))
        by_plat[plat] = {"views": sum(p["views"] or 0 for p in pp), "posts": len(pp),
                         "postsWithViews": sum(1 for p in pp if p["views"] is not None),
                         "accounts": len(accs), "status": status}
    by_plat["fb"] = {"views": None, "posts": None, "accounts": sum(1 for a in accounts if a["platform"] == "fb"), "status": "via-ig"}
    per = {}
    for p in posts:
        c = per.setdefault(p["abbr"], {"abbr": p["abbr"], "client": "", "views": 0, "posts": 0, "postsWithViews": 0,
                                       "byPlatform": {}, "top": None})
        c["views"] += p["views"] or 0
        c["posts"] += 1
        c["postsWithViews"] += int(p["views"] is not None)
        b = c["byPlatform"].setdefault(p["platform"], {"views": 0, "posts": 0})
        b["views"] += p["views"] or 0
        b["posts"] += 1
        if c["top"] is None or sort_key(p) < sort_key(c["top"]):
            c["top"] = p
    for a in accounts:
        if a["abbr"] in per and a.get("client"):
            per[a["abbr"]]["client"] = a["client"]
    per_client = sorted(per.values(), key=lambda c: -c["views"])
    for c in per_client:
        t = c["top"]
        c["top"] = {k: t[k] for k in ("platform", "id", "title", "views", "permalink")} if t else None
    ranked = sorted(posts, key=sort_key)
    top = [dict(p, rank=i + 1) for i, p in enumerate([p for p in ranked if p["views"] is not None][:3])]
    slim = [{k: p[k] for k in ("platform", "abbr", "account", "id", "title", "views", "viewsExact", "likes", "comments", "postedAt", "permalink")}
            for p in ranked[:MAX_POSTS_IN_DOC]]
    return {"v": 1, "weekStart": win["weekStart"], "weekEnd": win["weekEnd"], "tz": win["tz"],
            "generatedAt": int(time.time() * 1000), "mode": mode, "viewsDefinition": "lifetime at collection", "tools": tools,
            "totals": {"views": sum(p["views"] or 0 for p in posts), "posts": len(posts),
                       "postsWithViews": sum(1 for p in posts if p["views"] is not None), "byPlatform": by_plat},
            "top": top, "perClient": per_client, "posts": slim,
            "accounts": [{k: a.get(k) for k in ("abbr", "platform", "account", "url", "status", "posts", "pages", "note")} for a in accounts],
            "prev": prev, "warnings": warnings}


# ---- thumbnails (sips, no Pillow) --------------------------------------------
def attach_thumbs(top):
    for p in top:
        p["thumb"] = None
        url = p.get("thumbUrl")
        if not url:
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA if p["platform"] == "ig" else UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read(2_000_000)
            with tempfile.TemporaryDirectory() as td:
                src, dst = os.path.join(td, "in.jpg"), os.path.join(td, "out.jpg")
                with open(src, "wb") as fh:
                    fh.write(raw)
                for w, q in ((THUMB_W, 55), (120, 45)):
                    subprocess.run(["/usr/bin/sips", "--resampleWidth", str(w), "--setProperty", "format", "jpeg",
                                    "--setProperty", "formatOptions", str(q), src, "--out", dst],
                                   capture_output=True, timeout=30)
                    if os.path.exists(dst) and os.path.getsize(dst) <= THUMB_MAX_BYTES:
                        with open(dst, "rb") as fh:
                            p["thumb"] = "data:image/jpeg;base64," + base64.b64encode(fh.read()).decode()
                        break
        except Exception:
            p["thumb"] = None


# ---- state + write --------------------------------------------------------------
def state_load():
    try:
        with open(STATE_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}


def state_save(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_PATH, "w") as fh:
        json.dump(st, fh, indent=1)


def slim_prev(payload):
    return {"weekStart": payload["weekStart"], "mode": payload["mode"],
            "totals": {"views": payload["totals"]["views"], "posts": payload["totals"]["posts"],
                       "byPlatform": {k: {"views": v.get("views"), "posts": v.get("posts"), "status": v.get("status")}
                                      for k, v in payload["totals"]["byPlatform"].items()}},
            "perClient": [{"abbr": c["abbr"], "views": c["views"], "posts": c["posts"]} for c in payload["perClient"]]}


def write(payload, args, key):
    data = json.dumps(payload)
    if len(data) > DOC_SOFT_LIMIT:
        payload["posts"] = []
        payload["warnings"].append("posts[] dropped: doc over soft size limit")
        data = json.dumps(payload)
    assert len(data) < DOC_HARD_LIMIT, "payload would exceed the 1 MiB Firestore doc cap"
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(payload, fh, indent=2)
        print(f"Wrote {args.out} ({len(data):,} bytes, mode={payload['mode']}, {payload['totals']['posts']} posts, "
              f"{payload['totals']['views']:,} views)")
        return True
    if not key:
        print("No sync key (expected ~/Library/Application Support/anomaly-social/synckey). Nothing written.")
        return False
    body = {"fields": {"syncKey": {"stringValue": key},
                       "at": {"integerValue": str(int(time.time() * 1000))},
                       "data": {"stringValue": data}}}
    try:
        http(FIRESTORE + "/hub/socialStats", method="PATCH", body=body)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            print("Firestore write refused (403) — the hub/socialStats syncKey rule isn't published yet. Stopping quietly.")
            return False
        raise
    print(f"Synced hub/socialStats ({len(data):,} bytes, mode={payload['mode']}, "
          f"{payload['totals']['posts']} posts, {payload['totals']['views']:,} views).")
    return True


# ---- main ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out"); ap.add_argument("--clients"); ap.add_argument("--week")
    ap.add_argument("--no-ig", action="store_true"); ap.add_argument("--no-thumbs", action="store_true")
    ap.add_argument("--ig-rounds", type=int, default=3); ap.add_argument("--update-ytdlp", action="store_true")
    args = ap.parse_args()
    if not preflight():
        return
    tools = {}
    if args.update_ytdlp:
        try:
            subprocess.run([YTDLP, "-U"], capture_output=True, timeout=120)
        except Exception:
            pass
    try:
        tools["ytdlp"] = subprocess.run([YTDLP, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        tools["ytdlp"] = "missing"

    key = load_sync_key()
    win = week_window(args)
    print(f"Week {win['weekStart']} → {win['weekEnd']} ({win['tz']})")
    accounts = load_accounts(args)
    if not accounts:
        sys.exit("No social accounts configured — add links in pipeline Settings. Nothing written.")
    for a in accounts:
        a.update(status="pending", posts=0, pages=0, note="")
    warnings, posts = [], []

    def run(a, fn):
        try:
            got, meta = fn(a, win)
            posts.extend(got)
            a.update(status="ok", posts=len(got), pages=meta.get("pages", 1), note=meta.get("note", ""))
            print(f"  {a['platform']:6} @{handle_of(a['platform'], a['url'])} ({a['abbr']}): {len(got)} in window")
        except Exception as e:
            a.update(status="error", note=f"{type(e).__name__}: {str(e)[:160]}")
            warnings.append(f"{a['platform']} {a['url']} ({a['abbr']}): {a['note']}")
            print(f"  {a['platform']:6} {a['url']} ({a['abbr']}): FAILED {a['note']}")

    for a in [x for x in accounts if x["platform"] == "yt"]:
        run(a, collect_youtube)
    for a in [x for x in accounts if x["platform"] == "tiktok"]:
        run(a, collect_tiktok)
    for a in accounts:
        if a["platform"] == "fb":
            a.update(status="via-ig", note="counted through Instagram")

    ig_accounts = [x for x in accounts if x["platform"] == "ig"]
    if args.no_ig:
        for a in ig_accounts:
            a.update(status="skipped", note="--no-ig")
        ig_accounts = []
    rounds = max(1, args.ig_rounds)
    now = datetime.now()
    if now.day == 1:
        rounds = 1
        warnings.append("ig: 1st of the month — the ad-picks job owns the IP budget, single round only")
    try:
        if time.time() - os.path.getmtime(ADPICKS_LOG) < 3 * 3600:
            for a in ig_accounts:
                a.update(status="skipped", note="ad-picks catch-up active")
            warnings.append("ig: ad-picks catch-up ran in the last 3h — Instagram skipped this run")
            ig_accounts = []
    except OSError:
        pass

    st = state_load()
    prev = st.get("prev") if (st.get("last") or {}).get("weekStart") == win["weekStart"] else st.get("last")

    def emit(mode):
        pending = [a for a in accounts if a["status"] == "pending"]
        if all(a["status"] in ("error", "blocked") for a in accounts if a["platform"] != "fb"):
            print("Every account failed — refusing to overwrite last week's doc.")
            return False
        payload = aggregate(posts, accounts, win, prev, mode, list(warnings), tools)
        if not args.no_thumbs:
            attach_thumbs(payload["top"])
        ok = write(payload, args, key)
        if ok and mode == "complete":
            st2 = {"last": slim_prev(payload), "prev": prev}
            state_save(st2)
        return ok

    for rnd in range(1, rounds + 1):
        pending = [a for a in ig_accounts if a["status"] == "pending"]
        if not pending:
            break
        blocked = False
        for a in pending:
            try:
                got, meta = collect_instagram(a, win)
                posts.extend(got)
                a.update(status="ok", posts=len(got), pages=meta["pages"])
                print(f"  ig     @{handle_of('ig', a['url'])} ({a['abbr']}): {len(got)} in window")
            except IgBlocked as e:
                blocked = True
                print(f"  ig     blocked ({e}) after this round's {sum(1 for x in ig_accounts if x['status']=='ok')} account(s)")
                break
            except Exception as e:
                a.update(status="error", note=f"{type(e).__name__}: {str(e)[:160]}")
                warnings.append(f"ig {a['url']} ({a['abbr']}): {a['note']}")
            time.sleep(random.uniform(*IG_DELAY))
        still = [a for a in ig_accounts if a["status"] == "pending"]
        if blocked and still and rnd < rounds and not args.out:
            warnings.append(f"ig: blocked in round {rnd}; {len(still)} account(s) retry in 2h")
            emit("partial")
            warnings.pop()
            time.sleep(IG_ROUND_WAIT)
        elif still:
            for a in still:
                a.update(status="blocked", note="Instagram rate limit — try again later")
            warnings.append(f"ig: {len(still)} account(s) blocked by Instagram's rate limit this run")
            break
    emit("complete")


if __name__ == "__main__":
    main()
