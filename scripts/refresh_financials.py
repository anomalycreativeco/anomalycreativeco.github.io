#!/usr/bin/env python3
"""
Refresh the financials dashboard end to end: QuickBooks → page → live.

    fetch_qbo.py   pull every report from the QuickBooks API
    build_...py    turn the snapshot into the page payload, checking it reconciles
    sync_...py     encrypt it into financials.html and publish

This is what the weekly schedule runs. It publishes only when asked: a bare run
refreshes and reports what changed but leaves the live page alone, so a first run
(or a run after QuickBooks changes a report) can be inspected before anybody sees
it. --publish is what the scheduled job passes.

Every step already refuses to continue on numbers that do not tie out — the P&L
must balance, months must reconcile with period totals, overhead plus net income
must equal revenue. This script adds nothing to that; it just stops at the first
failure so a bad refresh cannot reach the page.

Usage:
  python3 scripts/refresh_financials.py             # refresh, do not publish
  python3 scripts/refresh_financials.py --publish   # refresh and push live
  python3 scripts/refresh_financials.py --publish --quiet   # for the scheduler
"""

import argparse
import datetime as dt
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
LOG = os.path.expanduser("~/Library/Logs/anomaly-financials.log")


def log(msg, quiet=False, stream=None):
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    if not quiet:
        print(msg, file=stream or sys.stdout)


def run(step, argv, quiet):
    log(f"→ {step}", quiet)
    r = subprocess.run([sys.executable] + argv, cwd=REPO,
                       capture_output=True, text=True)
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    for line in out.splitlines():
        log("   " + line, quiet)
    if r.returncode != 0:
        for line in err.splitlines():
            log("   " + line, quiet, stream=sys.stderr)
        log(f"✗ {step} failed (exit {r.returncode}) — nothing published.",
            quiet, stream=sys.stderr)
        sys.exit(r.returncode)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--publish", action="store_true",
                    help="push the refreshed page live")
    ap.add_argument("--quiet", action="store_true",
                    help="log to file only; for scheduled runs")
    ap.add_argument("--start", help="period start (YYYY-MM-DD)")
    ap.add_argument("--end", help="period end (YYYY-MM-DD)")
    args = ap.parse_args()

    log("=" * 60, args.quiet)
    log("Financials refresh" + (" (publishing)" if args.publish else " (dry)"),
        args.quiet)

    fetch = ["scripts/fetch_qbo.py"]
    if args.start:
        fetch += ["--start", args.start]
    if args.end:
        fetch += ["--end", args.end]
    run("fetch from QuickBooks", fetch, args.quiet)
    run("build payload", ["scripts/build_financials_payload.py", "--source", "api"],
        args.quiet)
    run("encrypt into page",
        ["scripts/sync_financials.py"] + (["--push"] if args.publish else ["--dry-run"]),
        args.quiet)

    log("✓ done" + ("" if args.publish else " — not published (pass --publish)"),
        args.quiet)


if __name__ == "__main__":
    main()
