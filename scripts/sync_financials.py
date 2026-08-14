#!/usr/bin/env python3
"""
Encrypt the financials payload into financials.html and (optionally) publish it.

Companion to sync_revenue.py — same crypto, same passcode, same gate. The split is
in where the numbers come from: sync_revenue.py reads the Excel Deal Tracker (the
pipeline view), this one reads QuickBooks (the books view).

The payload is built by scripts/build_financials_payload.py from raw QuickBooks
report dumps. Those dumps and the plaintext payload live outside this repo — the
repo is a public GitHub Pages site, so only the encrypted blob is ever committed.

The passcode is never stored in this repo. It is read from, in order:
  1. $HUB_KEY
  2. macOS Keychain:  security find-generic-password -s anomaly-hub-key -w

Refresh procedure:
  1. In Claude Code, pull the QuickBooks reports and save them to SNAP_DIR:
       pl_2026.json   profit_loss_quickbooks_account, Jan 1 - today
       pl_2025.json   profit_loss_quickbooks_account, prior calendar year
       extra.json     balance sheet / A-R aging / cash flow / customers
  2. python3 scripts/build_financials_payload.py
  3. python3 scripts/sync_financials.py --push

Usage:
  python3 scripts/sync_financials.py              # write financials.html
  python3 scripts/sync_financials.py --dry-run    # report only, touch nothing
  python3 scripts/sync_financials.py --push       # write, commit, push
"""

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(REPO, "financials.html")
SNAP_DIR = os.path.expanduser("~/Dropbox/Anomaly Creative LLC/qbo-snapshots")
PAYLOAD = os.path.join(SNAP_DIR, "financials_payload.json")

PBKDF2_ITERS = 150_000  # must match financials.html

# Every key the page reads. Publishing a payload that is missing one of these
# would render an empty card rather than fail, so check before writing.
REQUIRED = ["asOf", "elapsed", "summary", "months", "monthlyIncome",
            "monthlyExpenses", "monthlyCogs", "monthlyNet", "expenseBreakdown",
            "priorYear", "balanceSheet", "arAging", "cashFlow", "cashTrend",
            "topCustomers", "overhead"]


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
            "-s anomaly-hub-key -w\n"
            "  Or export HUB_KEY for a one-off run."
        )


def gate_hash_from_page(html: str) -> str:
    m = re.search(r'const GATE_HASH="([0-9a-f]{64})"', html)
    if not m:
        sys.exit("Could not find GATE_HASH in financials.html.")
    return m.group(1)


def encrypt(payload: dict, passcode: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(16), os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", passcode.encode(), salt, PBKDF2_ITERS, 32)
    ct = AESGCM(key).encrypt(
        nonce, json.dumps(payload, separators=(",", ":")).encode(), None)
    return base64.b64encode(salt + nonce + ct).decode()


def decrypt(blob: str, passcode: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(blob)
    key = hashlib.pbkdf2_hmac("sha256", passcode.encode(), raw[:16], PBKDF2_ITERS, 32)
    return json.loads(AESGCM(key).decrypt(raw[16:28], raw[28:], None))


def previous_payload(html: str, passcode: str):
    """Whatever is published right now, so we can report what moved. None if new."""
    m = re.search(r'const BLOB="([^"]*)"', html)
    if not m or not m.group(1):
        return None
    try:
        return decrypt(m.group(1), passcode)
    except Exception:
        return None


def load_payload() -> dict:
    if not os.path.exists(PAYLOAD):
        sys.exit(f"No payload at {PAYLOAD}\n"
                 "  Run: python3 scripts/build_financials_payload.py")
    with open(PAYLOAD, encoding="utf-8") as f:
        payload = json.load(f)
    missing = [k for k in REQUIRED if k not in payload]
    if missing:
        sys.exit("Payload is missing: " + ", ".join(missing))

    # The page charts monthlyIncome against monthlyExpenses; if the two disagree
    # with the headline summary the picture and the KPI row would tell different
    # stories, which is worse than not publishing.
    s = payload["summary"]
    inc = sum(payload["monthlyIncome"])
    exp = sum(payload["monthlyExpenses"]) + sum(payload["monthlyCogs"])
    if abs(inc - s["income"]) > 1 or abs(exp - (s["expenses"] + s["cogs"])) > 1:
        sys.exit(f"Payload is internally inconsistent — months total "
                 f"${inc:,.0f}/${exp:,.0f} but the summary says "
                 f"${s['income']:,.0f}/${s['expenses'] + s['cogs']:,.0f}.")

    # Overhead is the headline the page leads with, so its parts must add up and
    # tie back to revenue: revenue - everything spent = what was kept.
    o = payload["overhead"]
    parts = o["labor"]["total"] + o["nonLabor"]["total"]
    if abs(parts - o["total"]) > 1:
        sys.exit(f"Overhead parts total ${parts:,.0f} but overhead is "
                 f"${o['total']:,.0f}.")
    if abs(o["labor"]["payroll"] + o["labor"]["contractor"]
           - o["labor"]["total"]) > 1:
        sys.exit("Payroll and contractors do not add up to total labour cost.")
    if abs(o["total"] + s["netIncome"] - s["income"]) > 1:
        sys.exit(f"Overhead ${o['total']:,.0f} plus net ${s['netIncome']:,.0f} "
                 f"does not equal revenue ${s['income']:,.0f}.")
    return payload


def money_line(p):
    s = p["summary"]
    return (f"${s['income']:,.0f} income, ${s['expenses']:,.0f} expenses, "
            f"${s['netIncome']:,.0f} net ({s['netMargin'] * 100:.1f}% margin)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be published, write nothing")
    ap.add_argument("--push", action="store_true",
                    help="commit financials.html and push")
    args = ap.parse_args()

    payload = load_payload()

    if args.dry_run:
        print(f"{payload['asOf']}: {money_line(payload)}")
        print(f"cash ${payload['balanceSheet']['cash']:,.0f}, "
              f"A/R ${payload['arAging']['total']:,.0f} "
              f"(${payload['arAging']['overdue']:,.0f} overdue)")
        for n in payload.get("dataNotes", []):
            print("NOTE:", n)
        return

    with open(PAGE, encoding="utf-8") as f:
        html = f.read()

    passcode = get_passcode()
    if hashlib.sha256(("anomaly-hub|" + passcode).encode()).hexdigest() != gate_hash_from_page(html):
        sys.exit("Passcode does not match the page gate — refusing to write. "
                 "Nothing was changed.")

    prev = previous_payload(html, passcode)

    blob = encrypt(payload, passcode)
    if decrypt(blob, passcode) != payload:      # round-trip before touching the page
        sys.exit("Encrypt/decrypt round-trip failed — refusing to write.")

    new_html, n = re.subn(r'const BLOB="[^"]*"', 'const BLOB="' + blob + '"',
                          html, count=1)
    if n != 1:
        sys.exit("Could not locate the BLOB line in financials.html.")
    with open(PAGE, "w", encoding="utf-8") as f:
        f.write(new_html)

    print(f"financials.html resynced — as of {payload['asOf']}: {money_line(payload)}.")
    if prev:
        d_net = payload["summary"]["netIncome"] - prev["summary"]["netIncome"]
        d_cash = payload["balanceSheet"]["cash"] - prev["balanceSheet"]["cash"]
        print(f"since the published version ({prev['asOf']}): "
              f"net income {d_net:+,.0f}, cash {d_cash:+,.0f}")
    else:
        print("NOTE: no readable previous payload, so no diff.")
    for note in payload.get("dataNotes", []):
        print("NOTE:", note)

    if args.push:
        git = ["git", "-C", REPO]
        if not subprocess.run(git + ["diff", "--quiet", "--", "financials.html"]).returncode:
            print("No change to financials.html — nothing to push.")
            return
        subprocess.run(git + ["add", "financials.html"], check=True)
        subprocess.run(
            git + ["commit", "-m",
                   f"Financials dashboard: resync from QuickBooks ({payload['asOf']})"],
            check=True,
        )
        subprocess.run(git + ["push"], check=True)
        print("Pushed to anomalycreativeco.github.io.")


if __name__ == "__main__":
    main()
