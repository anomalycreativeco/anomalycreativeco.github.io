#!/usr/bin/env python3
"""
Resync the Studio Hub revenue dashboard (analytics.html) from the Excel Deal Tracker.

Reads the "Analytics Dashboard" sheet of the Deal Tracker workbook, rebuilds the
payload that analytics.html's render() expects, encrypts it with the dashboard
passcode (AES-GCM / PBKDF2-SHA256, matching the WebCrypto code in the page), and
rewrites the `const BLOB="..."` line in place.

The passcode is never stored in this repo. It is read from, in order:
  1. $HUB_KEY
  2. macOS Keychain:  security find-generic-password -s anomaly-hub-key -w

Store it once with:
  security add-generic-password -a "$USER" -s anomaly-hub-key -w 'THE_PASSCODE'

Usage:
  python3 scripts/sync_revenue.py              # write analytics.html
  python3 scripts/sync_revenue.py --dry-run    # print the payload, touch nothing
  python3 scripts/sync_revenue.py --push       # write, commit analytics.html, push
"""

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import warnings

warnings.filterwarnings("ignore")

# ── config ────────────────────────────────────────────────────────────────────
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(REPO, "analytics.html")
XLSX = "/Users/danielpan/Dropbox/Anomaly Creative LLC/Deal Tracker.xlsx"
SHEET = "Analytics Dashboard"

# Yearly collection goals shown on the "projected vs goals" chart.
# `aggressive` is the stretch goal the run-rate card is measured against.
GOALS = {"conservative": 800_000, "moderate": 1_000_000, "aggressive": 1_200_000}

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

PBKDF2_ITERS = 150_000  # must match analytics.html


# ── passcode ──────────────────────────────────────────────────────────────────
def get_passcode() -> str:
    key = os.environ.get("HUB_KEY")
    if key:
        return key.strip()
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "anomaly-hub-key", "-w"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except Exception:
        sys.exit(
            "No dashboard passcode found.\n"
            "  Store it once:  security add-generic-password -a \"$USER\" "
            "-s anomaly-hub-key -w 'THE_PASSCODE'\n"
            "  Or export HUB_KEY for a one-off run."
        )


def gate_hash_from_page(html: str) -> str:
    m = re.search(r'const GATE_HASH="([0-9a-f]{64})"', html)
    if not m:
        sys.exit("Could not find GATE_HASH in analytics.html.")
    return m.group(1)


# ── workbook parsing ──────────────────────────────────────────────────────────
def load_grid():
    import openpyxl
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    if SHEET not in wb.sheetnames:
        sys.exit(f'Sheet "{SHEET}" not found in {XLSX}')
    ws = wb[SHEET]
    return [list(r) for r in ws.iter_rows(values_only=True)]


def cell(row, i):
    return row[i] if i < len(row) else None


def txt(v):
    return str(v).strip() if v is not None else ""


def num(v):
    return round(float(v)) if isinstance(v, (int, float)) else 0


def find_row(grid, col, label, start=0):
    for i in range(start, len(grid)):
        if txt(cell(grid[i], col)).upper().startswith(label.upper()):
            return i
    return -1


def pairs_after(grid, header_row, name_col, val_col):
    """Rows following a header row, until the name column goes blank."""
    out = []
    for r in grid[header_row + 1:]:
        n = txt(cell(r, name_col))
        if not n:
            break
        out.append([n, num(cell(r, val_col))])
    return out


def build_payload(grid):
    # summary
    def summary_val(label):
        i = find_row(grid, 0, label)
        return num(cell(grid[i], 1)) if i >= 0 else 0

    projected = summary_val("Total Projected Revenue")
    collected = summary_val("Total Collected Revenue")
    outstanding = summary_val("Outstanding Revenue")

    clients = 0
    for r in grid:
        for c in range(len(r)):
            if txt(cell(r, c)).upper().startswith("TOTAL CLIENTS"):
                clients = num(cell(r, c + 1))
                break
        if clients:
            break

    # months — rows whose first cell is a date
    proj_m, coll_m, month_dates = [], [], []
    for r in grid:
        v = cell(r, 0)
        if isinstance(v, dt.datetime) or isinstance(v, dt.date):
            month_dates.append(v)
            proj_m.append(num(cell(r, 1)))
            coll_m.append(num(cell(r, 2)))
        if len(proj_m) == 12:
            break
    if len(proj_m) != 12:
        sys.exit(f"Expected 12 month rows in '{SHEET}', found {len(proj_m)}.")

    # complete months only — the month in progress would drag the run rate down
    today = dt.date.today()
    elapsed = sum(
        1 for d in month_dates
        if (d.year, d.month) < (today.year, today.month)
    )
    elapsed = max(1, min(12, elapsed))

    # industries
    i = find_row(grid, 0, "REVENUE BY INDUSTRY")
    industries = pairs_after(grid, find_row(grid, 0, "Industry", i), 0, 1) if i >= 0 else []
    industries.sort(key=lambda x: -x[1])

    # service mix
    i = find_row(grid, 0, "CLIENT SERVICE BREAKDOWN")
    services = pairs_after(grid, find_row(grid, 0, "Service Type", i), 0, 1) if i >= 0 else []
    services.sort(key=lambda x: -x[1])

    # client ranking (column E/F block)
    i = find_row(grid, 4, "Client", 0)
    ranking = pairs_after(grid, i, 4, 5) if i >= 0 else []
    ranking = [c for c in ranking if c[1] > 0]
    ranking.sort(key=lambda x: -x[1])

    as_of = dt.date.fromtimestamp(os.path.getmtime(XLSX)).strftime("%b %-d, %Y")

    return {
        "asOf": as_of,
        "elapsed": elapsed,
        "summary": {
            "projected": projected,
            "collected": collected,
            "outstanding": outstanding,
            "clients": clients,
        },
        "months": MONTHS,
        "projected": proj_m,
        "collected": coll_m,
        "industries": industries,
        "services": services,
        "clientRanking": ranking,
        "goals": GOALS,
    }


# ── crypto (mirrors the WebCrypto code in analytics.html) ─────────────────────
def encrypt(payload: dict, passcode: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(16), os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", passcode.encode(), salt, PBKDF2_ITERS, 32)
    ct = AESGCM(key).encrypt(nonce, json.dumps(payload, separators=(",", ":")).encode(), None)
    return base64.b64encode(salt + nonce + ct).decode()


def decrypt(blob: str, passcode: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(blob)
    key = hashlib.pbkdf2_hmac("sha256", passcode.encode(), raw[:16], PBKDF2_ITERS, 32)
    return json.loads(AESGCM(key).decrypt(raw[16:28], raw[28:], None))


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the payload, write nothing")
    ap.add_argument("--push", action="store_true", help="commit analytics.html and push")
    args = ap.parse_args()

    grid = load_grid()
    payload = build_payload(grid)

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        rr = sum(payload["collected"][max(0, payload["elapsed"] - 3):payload["elapsed"]])
        rr = rr / min(3, payload["elapsed"]) * 12
        print(f"\nrun rate (3-mo avg annualised): ${rr:,.0f}  "
              f"= {rr / GOALS['aggressive'] * 100:.0f}% of the "
              f"${GOALS['aggressive']:,} stretch goal")
        return

    with open(PAGE, encoding="utf-8") as f:
        html = f.read()

    passcode = get_passcode()
    if hashlib.sha256(("anomaly-hub|" + passcode).encode()).hexdigest() != gate_hash_from_page(html):
        sys.exit("Passcode does not match the dashboard gate — refusing to write. "
                 "Nothing was changed.")

    blob = encrypt(payload, passcode)
    if decrypt(blob, passcode) != payload:          # round-trip before we touch the page
        sys.exit("Encrypt/decrypt round-trip failed — refusing to write.")

    new_html, n = re.subn(r'const BLOB="[^"]*"', 'const BLOB="' + blob + '"', html, count=1)
    if n != 1:
        sys.exit("Could not locate the BLOB line in analytics.html.")
    with open(PAGE, "w", encoding="utf-8") as f:
        f.write(new_html)

    s = payload["summary"]
    print(f"analytics.html resynced — as of {payload['asOf']}: "
          f"${s['collected']:,} collected of ${s['projected']:,} projected, "
          f"{s['clients']} clients, {payload['elapsed']} months elapsed.")

    if args.push:
        git = ["git", "-C", REPO]
        if not subprocess.run(git + ["diff", "--quiet", "--", "analytics.html"]).returncode:
            print("No change to analytics.html — nothing to push.")
            return
        subprocess.run(git + ["add", "analytics.html"], check=True)
        subprocess.run(
            git + ["commit", "-m", f"Revenue dashboard: resync from Deal Tracker ({payload['asOf']})"],
            check=True,
        )
        subprocess.run(git + ["push"], check=True)
        print("Pushed to anomalycreativeco.github.io.")


if __name__ == "__main__":
    main()
