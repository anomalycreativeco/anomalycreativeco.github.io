#!/usr/bin/env python3
"""
Pull every figure the financials dashboard needs straight from QuickBooks.

This replaces the hand-driven flow where a Claude session called the QuickBooks
connector and saved its responses by hand. It also reaches the one thing that
connector cannot see at all — per-vendor expense detail — so contractor spend no
longer needs a CSV export.

Output is a single normalised snapshot, qbo_snapshot.json, which
build_financials_payload.py turns into the encrypted page payload. Keeping fetch
and analysis apart means a refresh can be re-run and diffed without touching the
published page, and the builder's reconciliation guards still get the last word on
whether anything is fit to publish.

A note on the P&L: it is requested with summarize_column_by=Month, so one request
returns the account tree AND the month-by-month split, already consistent with each
other. The connector could not do that, which is why the old path had to rebuild
monthly totals from a flattened dictionary and check them against the tree.

Usage:
  python3 scripts/fetch_qbo.py                     # calendar year to date
  python3 scripts/fetch_qbo.py --start 2026-01-01 --end 2026-08-09
  python3 scripts/fetch_qbo.py --dry-run           # fetch and summarise, write nothing
"""

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qbo_api import (QBOError, column_titles, company_name, num,  # noqa: E402
                     refresh_access_token, report, walk)

SNAP_DIR = os.path.expanduser("~/Dropbox/Anomaly Creative LLC/qbo-snapshots")
OUT = os.path.join(SNAP_DIR, "qbo_snapshot.json")

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# QuickBooks report names. Kept together because a wrong name is the most likely
# first-run failure and this is the one place to correct it.
R_PL = "ProfitAndLoss"
R_BS = "BalanceSheet"
R_CF = "CashFlow"
R_AR = "AgedReceivables"
R_CUST = "CustomerSales"
R_VENDOR = "VendorExpenses"

# Top-level P&L sections that are the cost of employed people. Mirrors
# PAYROLL_SECTIONS in build_financials_payload.py — see the note there about
# "Taxes paid" holding only employer payroll taxes in this book.
PAYROLL_SECTIONS = {"Payroll expenses", "Taxes paid", "Employee benefits"}
CONTRACTOR_SECTIONS = {"Contract labor"}

# Accounts that mean "a contractor was paid". Anything on the vendor report that
# does not touch these is somebody else — a landlord, a software vendor — and is
# not counted as contract labour.
CONTRACTOR_ACCOUNT_HINTS = ("contract labor", "contract labour", "subcontractor")


def month_columns(rep):
    """
    Map each report column to a calendar month index.

    Column titles come back as "Jan 2026" style labels with a leading blank column
    for account names and usually a trailing "Total". Returns {col_index: month_idx}
    covering only the real month columns.
    """
    out = {}
    for i, title in enumerate(column_titles(rep)):
        t = (title or "").strip()
        if not t or t.lower().startswith("total"):
            continue
        head = t.split()[0][:3].title()
        if head in MONTHS:
            # values[] in walk() excludes the label column, so shift by one
            out[i - 1] = MONTHS.index(head)
    return out


def section_rows(rep):
    return list(walk((rep.get("Rows") or {}).get("Row")))


def find_total(rows, *labels):
    """
    The values of a section's total row, matched on several spellings.

    Summary rows are searched first and header rows only as a fallback: a section
    header repeats the section's name but carries no figures, so matching it first
    silently returns a row of blanks — which reads as a legitimate zero rather
    than as a miss.
    """
    want = {l.lower() for l in labels}
    want |= {"total " + l for l in want}

    def matches(label):
        lab = (label or "").strip().lower()
        return lab in want or lab.replace("total ", "") in want

    def has_values(vals):
        return any(str(v).strip() != "" for v in vals or [])

    for _depth, label, vals, is_sum in rows:
        if is_sum and matches(label) and has_values(vals):
            return vals
    for _depth, label, vals, _is_sum in rows:
        if matches(label) and has_values(vals):
            return vals
    return None


def last_value(vals):
    """The Total column — QuickBooks puts it last when a report is split by month."""
    if not vals:
        return 0.0
    for v in reversed(vals):
        if str(v).strip() != "":
            return num(v)
    return 0.0


def build_pl(rep):
    """
    Turn the month-split P&L into totals, monthly series and an account breakdown.

    Section membership is tracked by depth: a row deeper than a known top-level
    section belongs to it. That is how contract labour and payroll get separated
    from everything else without hard-coding the whole chart of accounts.
    """
    rows = section_rows(rep)
    cols = month_columns(rep)

    def monthly(vals):
        out = [0.0] * 12
        for ci, mi in cols.items():
            if ci < len(vals):
                out[mi] = round(num(vals[ci]), 2)
        return out

    totals = {}
    for key, *labels in (
        ("income", "Income", "Total Income"),
        ("cogs", "Cost of Goods Sold", "Total Cost of Goods Sold"),
        ("gross", "Gross Profit", "Total Gross Profit"),
        ("expenses", "Expenses", "Total Expenses"),
        ("other", "Other Expenses", "Total Other Expenses"),
        ("net", "Net Income", "Total Net Income"),
    ):
        v = find_total(rows, *labels)
        totals[key] = round(last_value(v), 2) if v else 0.0
        totals[key + "_monthly"] = monthly(v) if v else [0.0] * 12

    # Walk the expense sections, attributing each top-level row and splitting the
    # people cost out of it.
    breakdown, payroll_m, contractor_m = [], [0.0] * 12, [0.0] * 12
    payroll_total = contractor_total = 0.0
    labor_detail = []

    in_expenses = False
    expense_depth = None
    current_top = None

    for depth, label, vals, is_sum in rows:
        lab = (label or "").strip()
        low = lab.lower()

        if low in ("expenses", "other expenses"):
            in_expenses, expense_depth, current_top = True, depth, None
            continue
        if low.startswith("total ") and low.replace("total ", "") in ("expenses", "other expenses"):
            in_expenses, current_top = False, None
            continue
        if not in_expenses or expense_depth is None:
            continue

        top_level = depth == expense_depth + 1
        if top_level and not is_sum:
            current_top = lab
            # a leaf top-level row carries its own money; a group's money arrives
            # on its "Total X" summary a few rows later
            if any(str(v).strip() for v in vals):
                amt = last_value(vals)
                if amt:
                    breakdown.append([lab, round(amt, 2)])
                    if lab in PAYROLL_SECTIONS:
                        payroll_total += amt
                        for i, m in enumerate(monthly(vals)):
                            payroll_m[i] += m
                    elif lab in CONTRACTOR_SECTIONS:
                        contractor_total += amt
                        for i, m in enumerate(monthly(vals)):
                            contractor_m[i] += m
                    if lab in PAYROLL_SECTIONS or lab in CONTRACTOR_SECTIONS:
                        labor_detail.append([lab, round(amt, 2)])
        elif is_sum and current_top and low == ("total " + current_top.lower()):
            amt = last_value(vals)
            if amt:
                breakdown.append([current_top, round(amt, 2)])
                if current_top in PAYROLL_SECTIONS:
                    payroll_total += amt
                    for i, m in enumerate(monthly(vals)):
                        payroll_m[i] += m
                elif current_top in CONTRACTOR_SECTIONS:
                    contractor_total += amt
                    for i, m in enumerate(monthly(vals)):
                        contractor_m[i] += m
            current_top = None
        elif current_top in PAYROLL_SECTIONS and not is_sum and depth > expense_depth + 1:
            amt = last_value(vals)
            if amt:
                labor_detail.append([lab, round(amt, 2)])

    # de-duplicate the breakdown, keeping the largest figure seen for each account
    merged = {}
    for name, amt in breakdown:
        merged[name] = max(merged.get(name, 0), amt)

    return {
        "totals": {k: v for k, v in totals.items() if not k.endswith("_monthly")},
        "monthlyIncome": totals["income_monthly"],
        "monthlyCogs": totals["cogs_monthly"],
        "monthlyExpenses": [round(totals["expenses_monthly"][i]
                                  + totals["other_monthly"][i], 2) for i in range(12)],
        "expenseBreakdown": sorted(([k, v] for k, v in merged.items()),
                                   key=lambda x: -x[1]),
        "payroll": round(payroll_total, 2),
        "contractor": round(contractor_total, 2),
        "monthlyPayroll": [round(v, 2) for v in payroll_m],
        "monthlyContractor": [round(v, 2) for v in contractor_m],
        "laborDetail": sorted(labor_detail, key=lambda x: -x[1]),
    }


def build_balance_sheet(rep):
    rows = section_rows(rep)

    def val(*labels):
        v = find_total(rows, *labels)
        return round(last_value(v), 2) if v else 0.0

    assets = val("Total Assets", "Assets")
    liabilities = val("Total Liabilities", "Liabilities")
    equity = val("Total Equity", "Equity")
    current_assets = val("Total Current Assets", "Current Assets")
    current_liab = val("Total Current Liabilities", "Current Liabilities")
    return {
        "totalAssets": assets,
        "totalLiabilities": liabilities,
        "totalEquity": equity,
        "cash": val("Total Bank Accounts", "Bank Accounts"),
        "accountsReceivable": val("Total Accounts Receivable", "Accounts Receivable"),
        "otherCurrentAssets": val("Total Other Current Assets", "Other Current Assets"),
        "fixedAssets": val("Total Fixed Assets", "Fixed Assets"),
        "otherAssets": val("Total Other Assets", "Other Assets"),
        "currentAssets": current_assets,
        "currentLiabilities": current_liab,
        "accountsPayable": val("Total Accounts Payable", "Accounts Payable"),
        "creditCards": val("Total Credit Cards", "Credit Cards"),
        "otherCurrentLiabilities": val("Total Other Current Liabilities",
                                       "Other Current Liabilities"),
        "longTermLiabilities": val("Total Long-Term Liabilities",
                                   "Long-Term Liabilities", "Long Term Liabilities"),
        "currentRatio": round(current_assets / current_liab, 2) if current_liab else 0,
        "debtToEquity": round(liabilities / equity, 2) if equity else 0,
        "workingCapital": round(current_assets - current_liab, 2),
    }


def build_cash_flow(rep):
    rows = section_rows(rep)

    def val(*labels):
        v = find_total(rows, *labels)
        return round(last_value(v), 2) if v else 0.0

    return {
        "operating": val("Net cash provided by operating activities",
                         "Total Operating Activities", "OPERATING ACTIVITIES"),
        "investing": val("Net cash provided by investing activities",
                         "Total Investing Activities", "INVESTING ACTIVITIES"),
        "financing": val("Net cash provided by financing activities",
                         "Total Financing Activities", "FINANCING ACTIVITIES"),
        "netChange": val("Net cash increase for period",
                         "NET CASH INCREASE FOR PERIOD"),
        "cashAtBeginning": val("Cash at beginning of period"),
        "cashAtEnd": val("Cash at end of period", "CASH AT END OF PERIOD"),
        "ownerDistributions": val("Owner's distribution", "Owners distribution"),
    }


def build_ar(rep):
    """A/R aging: bucket totals from the report's own Total row, plus per-customer."""
    rows = section_rows(rep)
    titles = [t for t in column_titles(rep)[1:] if t]
    total_row = None
    customers = []
    for _d, label, vals, is_sum in rows:
        lab = (label or "").strip()
        if lab.lower().startswith("total"):
            total_row = vals
        elif lab and any(str(v).strip() for v in vals) and not is_sum:
            customers.append([lab, round(last_value(vals), 2)])

    buckets = []
    if total_row:
        for i, t in enumerate(titles):
            if t.lower().startswith("total"):
                continue
            if i < len(total_row):
                buckets.append([t, round(num(total_row[i]), 2)])
    total = round(last_value(total_row), 2) if total_row else 0.0
    current = buckets[0][1] if buckets else 0.0
    overdue = round(total - current, 2)
    return {
        "total": total,
        "current": current,
        "overdue": overdue,
        "overduePercent": round(overdue / total * 100, 1) if total else 0,
        "buckets": buckets,
        "customerCount": len(customers),
        "topOverdue": sorted(customers, key=lambda x: -x[1])[:5],
    }


def build_customers(rep, top_n=15):
    rows = section_rows(rep)
    out = []
    for _d, label, vals, is_sum in rows:
        lab = (label or "").strip()
        if not lab or is_sum or lab.lower().startswith("total") or lab.lower() == "not specified":
            continue
        amt = last_value(vals)
        if amt:
            out.append([lab, round(amt, 2)])
    out.sort(key=lambda x: -x[1])
    total = round(sum(v for _, v in out), 2)
    top5 = round(sum(v for _, v in out[:5]) / total * 100, 1) if total else 0
    return {"rows": out[:top_n], "count": len(out), "total": total,
            "top5Concentration": top5}


def build_contractors(rep, account_total):
    """
    Per-vendor contract labour, from the vendor expense report.

    The report covers every vendor, so it is filtered to rows whose account is
    contract labour. When the report carries no account column the filter cannot
    run, and the reconciliation against the Contract labor account total is what
    reveals it — a wildly-too-large named total means everything got counted.
    """
    rows = section_rows(rep)
    titles = [t.lower() for t in column_titles(rep)]
    account_cols = [i - 1 for i, t in enumerate(titles)
                    if any(h in t for h in CONTRACTOR_ACCOUNT_HINTS)]

    out = []
    for _d, label, vals, is_sum in rows:
        lab = (label or "").strip()
        if not lab or is_sum or lab.lower().startswith("total") or lab.lower() == "not specified":
            continue
        amt = (round(sum(num(vals[c]) for c in account_cols if c < len(vals)), 2)
               if account_cols else round(last_value(vals), 2))
        if amt:
            out.append([lab, amt])
    out.sort(key=lambda x: -x[1])

    named = round(sum(v for _, v in out), 2)
    gap = round(account_total - named, 2)
    return {
        "rows": out,
        "count": len(out),
        "named": named,
        "accountTotal": round(account_total, 2),
        "unaccounted": gap,
        "complete": abs(gap) < 1,
        "source": R_VENDOR + (" (account-filtered)" if account_cols else " (all accounts)"),
        "accountFiltered": bool(account_cols),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", help="period start (YYYY-MM-DD), default Jan 1 this year")
    ap.add_argument("--end", help="period end (YYYY-MM-DD), default today")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and summarise, write nothing")
    args = ap.parse_args()

    today = dt.date.today()
    start = args.start or f"{today.year}-01-01"
    end = args.end or today.isoformat()
    py_start, py_end = f"{today.year - 1}-01-01", f"{today.year - 1}-12-31"

    try:
        access, realm = refresh_access_token()
        name = company_name(access, realm)
        print(f"{name} — {start} to {end}")

        def rep(n, **p):
            return report(n, access=access, realm=realm, **p)

        pl = build_pl(rep(R_PL, start_date=start, end_date=end,
                          accounting_method="Accrual", summarize_column_by="Month"))
        prior = build_pl(rep(R_PL, start_date=py_start, end_date=py_end,
                             accounting_method="Accrual", summarize_column_by="Month"))
        bs = build_balance_sheet(rep(R_BS, start_date=start, end_date=end,
                                     accounting_method="Accrual"))
        cf = build_cash_flow(rep(R_CF, start_date=start, end_date=end))
        ar = build_ar(rep(R_AR, report_date=end))
        cust = build_customers(rep(R_CUST, start_date=start, end_date=end,
                                   accounting_method="Accrual"))
        con = build_contractors(rep(R_VENDOR, start_date=start, end_date=end,
                                    accounting_method="Accrual"), pl["contractor"])
    except QBOError as e:
        sys.exit(str(e))

    snap = {
        "fetchedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "company": name,
        "periodStart": start,
        "periodEnd": end,
        "pl": pl,
        "priorYear": {"label": str(today.year - 1),
                      "income": prior["totals"]["income"],
                      "netIncome": prior["totals"]["net"],
                      "monthlyIncome": prior["monthlyIncome"]},
        "balanceSheet": bs,
        "cashFlow": cf,
        "arAging": ar,
        "customers": cust,
        "contractors": con,
    }

    t = pl["totals"]
    print(f"  income      ${t['income']:>12,.2f}")
    print(f"  expenses    ${t['expenses'] + t['other']:>12,.2f}")
    print(f"  net income  ${t['net']:>12,.2f}")
    print(f"  payroll     ${pl['payroll']:>12,.2f}")
    print(f"  contractors ${pl['contractor']:>12,.2f}  "
          f"({con['count']} named, ${con['unaccounted']:,.2f} unaccounted)")
    print(f"  cash        ${bs['cash']:>12,.2f}")

    # The identity that must hold for any P&L: what came in, less what went out,
    # is what was kept. If it fails the report was parsed wrongly and nothing
    # downstream should trust it.
    spent = t["expenses"] + t["other"] + t["cogs"]
    if abs(t["income"] - spent - t["net"]) > 1:
        sys.exit(f"\nParsed P&L does not balance: income ${t['income']:,.2f} "
                 f"- spend ${spent:,.2f} != net ${t['net']:,.2f}. "
                 f"Report shape has changed — inspect with:\n"
                 f"  python3 scripts/qbo_api.py --raw {R_PL} --outline "
                 f"--param start_date={start} --param end_date={end}")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return
    os.makedirs(SNAP_DIR, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=2)
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
