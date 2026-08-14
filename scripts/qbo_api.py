#!/usr/bin/env python3
"""
Talk to the QuickBooks Online REST API directly.

The Claude QuickBooks connector cannot see everything the books hold — there is no
expenses-by-vendor report, so per-contractor spend is invisible to it, and every
refresh needs a human in a chat session. This module goes at the API underneath,
which does expose those reports, so the dashboard can refresh itself.

CREDENTIALS — stored in the macOS Keychain, never in this repo:

    anomaly-qbo-client    "<client_id>:<client_secret>"   from developer.intuit.com
    anomaly-qbo-refresh   "<refresh_token>"               from the one-time OAuth grant
    anomaly-qbo-realm     "<realm_id>"                    the company id (optional,
                                                          defaults to REALM_ID below)

Store them once:

    security add-generic-password -a "$USER" -s anomaly-qbo-client  -w
    security add-generic-password -a "$USER" -s anomaly-qbo-refresh -w

Each may be overridden for a one-off run with $QBO_CLIENT, $QBO_REFRESH, $QBO_REALM.

TOKEN ROTATION: Intuit issues a new refresh token on nearly every exchange and
invalidates the old one, so a rotated token MUST be written back or the next run is
locked out. `refresh_access_token` writes it back to the Keychain immediately. The
token also dies after ~100 days unused, which the weekly schedule keeps alive.

Usage:
  python3 scripts/qbo_api.py --check              # verify auth, print the company
  python3 scripts/qbo_api.py --raw ProfitAndLoss  # dump one report's raw JSON
  python3 scripts/qbo_api.py --raw VendorExpenses --param start_date=2026-01-01
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

# The company this dashboard reports on. Taken from the QuickBooks connector's own
# responses; override with $QBO_REALM or the anomaly-qbo-realm Keychain item.
REALM_ID = "9130356010607356"

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
API_BASE = "https://quickbooks.api.intuit.com/v3/company"

# Intuit's sandbox lives on a different host; flip with $QBO_SANDBOX=1 if a test
# company is ever needed.
if os.environ.get("QBO_SANDBOX"):
    API_BASE = "https://sandbox-quickbooks.api.intuit.com/v3/company"


class QBOError(RuntimeError):
    pass


# ── credentials ───────────────────────────────────────────────────────────────
def keychain_read(service):
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except Exception:
        return None


def keychain_write(service, value):
    """Replace a Keychain item. Used to persist rotated refresh tokens."""
    user = os.environ.get("USER", "")
    subprocess.run(["security", "delete-generic-password", "-s", service],
                   capture_output=True, text=True)
    r = subprocess.run(
        ["security", "add-generic-password", "-a", user, "-s", service,
         "-w", value, "-U"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise QBOError(f"Could not save {service} to the Keychain: {r.stderr.strip()}")


def credentials():
    raw = os.environ.get("QBO_CLIENT") or keychain_read("anomaly-qbo-client")
    if not raw or ":" not in raw:
        raise QBOError(
            "No QuickBooks client credentials.\n"
            "  Create an app at developer.intuit.com, then store its keys:\n"
            '    security add-generic-password -a "$USER" -s anomaly-qbo-client -w\n'
            '  (enter "<client_id>:<client_secret>" when prompted)')
    client_id, client_secret = raw.split(":", 1)

    refresh = os.environ.get("QBO_REFRESH") or keychain_read("anomaly-qbo-refresh")
    if not refresh:
        raise QBOError(
            "No QuickBooks refresh token.\n"
            "  Authorize the app once against the company, then store the token:\n"
            '    security add-generic-password -a "$USER" -s anomaly-qbo-refresh -w')

    realm = (os.environ.get("QBO_REALM") or keychain_read("anomaly-qbo-realm")
             or REALM_ID)
    return client_id.strip(), client_secret.strip(), refresh.strip(), realm.strip()


# ── auth ──────────────────────────────────────────────────────────────────────
def refresh_access_token():
    """
    Exchange the refresh token for an access token.

    Intuit rotates the refresh token on this call, so the new one is written back
    before anything else can fail — losing a rotated token means re-authorising by
    hand, which defeats the point of an unattended refresh.
    """
    import base64
    client_id, client_secret, refresh, realm = credentials()

    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh,
    }).encode()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={
        "Authorization": "Basic " + basic,
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            tok = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        if e.code in (400, 401):
            raise QBOError(
                f"QuickBooks rejected the refresh token ({e.code}). It has either "
                f"expired (they die after ~100 days unused) or been rotated away by "
                f"another run. Re-authorise the app and store the new token.\n"
                f"  {detail}")
        raise QBOError(f"Token exchange failed ({e.code}): {detail}")

    new_refresh = tok.get("refresh_token")
    if new_refresh and new_refresh != refresh and not os.environ.get("QBO_REFRESH"):
        keychain_write("anomaly-qbo-refresh", new_refresh)

    access = tok.get("access_token")
    if not access:
        raise QBOError(f"No access token in the response: {tok}")
    return access, realm


# ── requests ──────────────────────────────────────────────────────────────────
def api_get(path, params=None, access=None, realm=None):
    if access is None or realm is None:
        access, realm = refresh_access_token()
    url = f"{API_BASE}/{realm}/{path.lstrip('/')}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + access,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise QBOError(f"GET {path} failed ({e.code}): {detail}")


def report(name, access=None, realm=None, **params):
    """
    Fetch one report.

    Common params: start_date, end_date (YYYY-MM-DD), accounting_method
    (Accrual/Cash), summarize_column_by (Month/Quarter/Year), minorversion.
    """
    params.setdefault("minorversion", "70")
    return api_get(f"reports/{name}", params, access=access, realm=realm)


def company_name(access=None, realm=None):
    if access is None or realm is None:
        access, realm = refresh_access_token()
    info = api_get("companyinfo/" + realm, {"minorversion": "70"},
                   access=access, realm=realm)
    return (info.get("CompanyInfo") or {}).get("CompanyName", "(unnamed)")


# ── report shape helpers ──────────────────────────────────────────────────────
def column_titles(rep):
    return [c.get("ColTitle", "") for c in (rep.get("Columns") or {}).get("Column", [])]


def walk(rows, depth=0):
    """
    Flatten QuickBooks' nested report rows.

    A report row is either a Section (a header, nested Rows, and usually a Summary)
    or a plain Data row. Both carry their values in ColData. Yields
    (depth, label, [values...], is_summary) so callers can pick the level they need
    without re-implementing the nesting each time.
    """
    for row in rows or []:
        rtype = row.get("type")
        if rtype == "Section":
            head = row.get("Header", {}).get("ColData")
            if head:
                yield depth, head[0].get("value", ""), [c.get("value", "") for c in head[1:]], False
            yield from walk((row.get("Rows") or {}).get("Row"), depth + 1)
            summ = row.get("Summary", {}).get("ColData")
            if summ:
                yield depth, summ[0].get("value", ""), [c.get("value", "") for c in summ[1:]], True
        else:
            cd = row.get("ColData")
            if cd:
                yield depth, cd[0].get("value", ""), [c.get("value", "") for c in cd[1:]], False


def num(v):
    if v is None or v == "":
        return 0.0
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except ValueError:
        return 0.0


# ── cli ───────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="verify credentials and print the connected company")
    ap.add_argument("--raw", metavar="REPORT",
                    help="dump one report's raw JSON (e.g. ProfitAndLoss)")
    ap.add_argument("--param", action="append", default=[], metavar="K=V",
                    help="extra report parameter; repeatable")
    ap.add_argument("--outline", action="store_true",
                    help="with --raw, print the row outline instead of full JSON")
    args = ap.parse_args()

    try:
        if args.check:
            access, realm = refresh_access_token()
            print(f"Connected to: {company_name(access, realm)}  (realm {realm})")
            print("Credentials are good; the refresh token has been rotated and saved.")
            return

        if args.raw:
            params = {}
            for p in args.param:
                if "=" in p:
                    k, v = p.split("=", 1)
                    params[k] = v
            rep = report(args.raw, **params)
            if args.outline:
                print(f"columns: {column_titles(rep)}")
                for depth, label, vals, is_sum in walk((rep.get("Rows") or {}).get("Row")):
                    tag = " [sum]" if is_sum else ""
                    print("  " * depth + f"{label}{tag}  {vals[:4]}")
            else:
                print(json.dumps(rep, indent=2))
            return

        ap.print_help()
    except QBOError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
