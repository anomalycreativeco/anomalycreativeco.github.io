#!/usr/bin/env python3
"""
Build the Studio Hub financials payload from raw QuickBooks MCP report dumps.

The QuickBooks MCP tools hand back a lot of derived fields that are wrong, so this
script deliberately ignores most of them and reads the raw QBO report tree instead:

  * `totalExpenses` at the top level comes back as 0.
  * `expenseAccountsAggregated` double-counts every group: the parent row carries
    the same money as its "Total for X" child, so summing the dict inflates
    expenses by roughly the share of spend that sits inside groups.

The trustworthy figures live in `reportData.data.rows`, which is QBO's own report
tree — parent groups, "Total for X" rows and leaf accounts, each tagged with a
dotted id ("4.15.2" is the second child of the fifteenth child of the fourth
top-level row). That tree is parsed for the period totals and, crucially, for the
*shape* of the chart of accounts: which top-level expense rows are groups and which
are standalone leaves.

That shape is then applied to the per-month dictionaries to get correct monthly
expense totals — a group contributes its "Total for X" entry, a leaf contributes
itself, and nothing gets counted twice.

Inputs (raw tool output saved to disk, never committed — see SNAP_DIR):
  pl_2026.json     profit_loss_quickbooks_account, Jan 1 - today
  pl_2025.json     profit_loss_quickbooks_account, prior calendar year
  extra.json       hand-assembled: balance sheet, A/R aging, cash flow, customers

Output:
  financials_payload.json   the plaintext payload; sync_financials.py encrypts it

Usage:
  python3 scripts/build_financials_payload.py
  python3 scripts/build_financials_payload.py --print
"""

import argparse
import datetime as dt
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_DIR = os.path.expanduser("~/Dropbox/Anomaly Creative LLC/qbo-snapshots")
OUT = os.path.join(SNAP_DIR, "financials_payload.json")

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# QuickBooks' fiscal year is currently set to start in August, which is why the
# balance sheet's equity section splits the year in an unexpected place. Every
# range here is an explicit calendar date, so the setting cannot reach these
# numbers — but the page still shows retained earnings and net income combined
# rather than trusting QBO's split. See the note surfaced in the payload.
FISCAL_YEAR_NOTE = ("QuickBooks fiscal year starts in August; all figures here are "
                    "calendar-year to date.")


def load(name):
    path = os.path.join(SNAP_DIR, name)
    if not os.path.exists(path):
        sys.exit(f"Missing snapshot: {path}\n"
                 "Refresh the QuickBooks reports and save them there first.")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cell(row, name):
    for c in row.get("cells", []):
        if c.get("name") == name:
            return c.get("value")
    return None


def account_name(row):
    return cell(row, "ACCOUNT_NAME")


def amount(row):
    v = cell(row, "DETAIL_NATURAL_HOME_AMOUNT__TOTAL")
    return v if isinstance(v, (int, float)) else 0


def tree_rows(pl):
    return pl["reportData"]["data"]["rows"]


def find_section(rows, label):
    """The top-level row whose account name is `label`, plus its id prefix."""
    for r in rows:
        if account_name(r) == label:
            return r
    return None


def children_of(rows, parent_id):
    """Direct children of a row id — ids are dotted paths like '4.15'."""
    depth = parent_id.count(".") + 1
    out = []
    for r in rows:
        rid = r["metadata"]["id"]
        if rid.startswith(parent_id + ".") and rid.count(".") == depth:
            out.append(r)
    return out


def leaf_names(rows, row):
    """Every descendant of `row` that has no children of its own."""
    kids = children_of(rows, row["metadata"]["id"])
    if not kids:
        name = account_name(row)
        return [name] if name else []
    out = []
    for k in kids:
        name = account_name(k)
        if name and name.startswith("Total for "):
            continue
        out.append(k)
    names = []
    for k in out:
        names.extend(leaf_names(rows, k))
    return names


def section_resolvers(rows, section_label):
    """
    How to read each top-level row of a P&L section out of a monthly dict.

    The monthly dictionaries are flat, and QuickBooks is not consistent about how
    it flattens: operating groups keep a "Total for <name>" entry, but Other
    Expenses is flattened all the way down to its leaves with no total row. So
    each resolver carries both routes — prefer the total, fall back to summing
    the group's own leaves — which reads either shape without double counting.
    """
    section = find_section(rows, section_label)
    if section is None:
        return []
    resolvers = []
    for row in children_of(rows, section["metadata"]["id"]):
        name = account_name(row)
        if not name or name.startswith("Total for "):
            continue
        if children_of(rows, row["metadata"]["id"]):
            resolvers.append({"total": "Total for " + name,
                              "leaves": leaf_names(rows, row)})
        else:
            resolvers.append({"total": None, "leaves": [name]})
    return resolvers


def month_total(month_accounts, resolvers):
    """Sum one month's flat dict using the shape derived from the report tree."""
    total = 0.0
    for r in resolvers:
        key = r["total"]
        if key and key in month_accounts:
            total += month_accounts.get(key) or 0
        else:
            for leaf in r["leaves"]:
                total += month_accounts.get(leaf, 0) or 0
    return total


def section_total(rows, label):
    section = find_section(rows, label)
    return round(amount(section), 2) if section else 0.0


def top_level_expenses(rows):
    """Every top-level expense line with its period total, largest first."""
    section = find_section(rows, "Expenses")
    if section is None:
        return []
    out = []
    for row in children_of(rows, section["metadata"]["id"]):
        name = account_name(row)
        if name and amount(row):
            out.append([name, round(amount(row), 2)])
    return sorted(out, key=lambda x: -x[1])


def data_notes(rows, income):
    """
    Caveats the page prints under the numbers.

    These are conditions in the books that make a headline figure read better (or
    worse) than reality. They are derived, not hard-coded, so a note disappears on
    its own once the underlying bookkeeping is cleaned up.
    """
    notes = []
    cogs = section_total(rows, "Cost of Goods Sold")
    labor = 0.0
    section = find_section(rows, "Expenses")
    if section:
        for row in children_of(rows, section["metadata"]["id"]):
            if account_name(row) == "Contract labor":
                labor = amount(row)
    if labor > cogs:
        notes.append(
            f"Gross margin reads high: ${labor:,.0f} of contract labor sits in "
            f"overhead rather than cost of sales, so only ${cogs:,.0f} counts "
            f"against revenue.")

    holding = 0.0
    if section:
        for row in children_of(rows, section["metadata"]["id"]):
            if account_name(row) == "Ask My Client":
                holding = amount(row)
    if holding:
        notes.append(
            f"${holding:,.0f} is still parked in \"Ask My Client\" awaiting "
            f"categorisation, so the expense split below will shift.")

    if income:
        for row in rows:
            if account_name(row) == "Discounts given" and amount(row):
                d = abs(amount(row))
                notes.append(
                    f"${d:,.0f} of discounts given ({d / income * 100:.1f}% of "
                    f"income) is netted into the revenue figure.")
                break
    return notes


def build():
    pl = load("pl_2026.json")
    prior = load("pl_2025.json")
    extra = load("extra.json")

    rows = tree_rows(pl)

    income = section_total(rows, "Income")
    cogs = section_total(rows, "Cost of Goods Sold")
    gross = section_total(rows, "Gross Profit")
    expenses = section_total(rows, "Expenses")
    other_exp = section_total(rows, "Other Expenses")
    net = section_total(rows, "Net Income")

    exp_res = section_resolvers(rows, "Expenses")
    other_res = section_resolvers(rows, "Other Expenses")
    cogs_res = section_resolvers(rows, "Cost of Goods Sold")

    monthly = pl.get("monthlyBreakdown", {})
    m_income = [0.0] * 12
    m_expense = [0.0] * 12
    m_cogs = [0.0] * 12
    elapsed = 0

    for key in sorted(monthly):
        start = key.split(" - ")[0]
        idx = int(start[5:7]) - 1
        block = monthly[key]
        m_income[idx] = round(block.get("totalIncome", 0) or 0, 2)
        # operating expenses + other expenses, both shaped from the tree
        acct = block.get("expenseAccounts", {}) or {}
        m_expense[idx] = round(month_total(acct, exp_res)
                               + month_total(acct, other_res), 2)
        m_cogs[idx] = round(
            month_total(block.get("cogsAccounts", {}) or {}, cogs_res), 2)
        elapsed = max(elapsed, idx + 1)

    m_net = [round(m_income[i] - m_cogs[i] - m_expense[i], 2) for i in range(12)]

    # The monthly dictionaries must reconstruct the period totals from the tree.
    # If they don't, QuickBooks changed its report shape and the page would ship
    # numbers nobody can tie out — so stop rather than publish them.
    checks = [("income", sum(m_income), income),
              ("expenses", sum(m_expense), expenses + other_exp),
              ("cogs", sum(m_cogs), cogs)]
    drift = [(what, got, want) for what, got, want in checks
             if abs(got - want) > 1.0]
    if drift:
        for what, got, want in drift:
            print(f"MISMATCH {what}: months sum to {got:,.2f}, "
                  f"report tree says {want:,.2f}", file=sys.stderr)
        sys.exit("Monthly figures do not reconcile with the report tree — "
                 "refusing to build a payload.")

    prior_rows = tree_rows(prior)
    prior_income = section_total(prior_rows, "Income")
    prior_net = section_total(prior_rows, "Net Income")

    # Year over year on complete months only. The current month is still being
    # written, so counting it against a full month last year would read as a
    # slowdown that hasn't happened.
    today = dt.date.today()
    complete = max(0, elapsed - 1) if today.day > 1 else elapsed
    prior_monthly = prior.get("monthlyBreakdown", {})
    prior_same = 0.0
    for key in sorted(prior_monthly):
        if int(key.split(" - ")[0][5:7]) <= complete:
            prior_same += prior_monthly[key].get("totalIncome", 0) or 0
    current_same = sum(m_income[:complete])

    bs = extra["balanceSheet"]
    payload = {
        "asOf": today.strftime("%b %-d, %Y"),
        "periodStart": pl.get("periodStart"),
        "periodEnd": pl.get("periodEnd"),
        "elapsed": elapsed,
        "note": FISCAL_YEAR_NOTE,
        "summary": {
            "income": round(income, 2),
            "cogs": round(cogs, 2),
            "grossProfit": round(gross, 2),
            "grossMargin": round(gross / income, 4) if income else 0,
            "expenses": round(expenses + other_exp, 2),
            "netIncome": round(net, 2),
            "netMargin": round(net / income, 4) if income else 0,
        },
        "months": MONTHS,
        "monthlyIncome": m_income,
        "monthlyExpenses": m_expense,
        "monthlyCogs": m_cogs,
        "monthlyNet": m_net,
        "expenseBreakdown": top_level_expenses(rows),
        "priorYear": {
            "label": str(today.year - 1),
            "income": round(prior_income, 2),
            "netIncome": round(prior_net, 2),
            "compareMonths": complete,
            "compareLabel": f"{MONTHS[0]}–{MONTHS[complete - 1]}" if complete else "",
            "incomeSamePeriod": round(prior_same, 2),
            "incomeThisPeriod": round(current_same, 2),
            "incomeChange": round(current_same - prior_same, 2),
            "incomeChangePct": round((current_same - prior_same) / prior_same, 4)
            if prior_same else 0,
        },
        "dataNotes": data_notes(rows, income),
        "balanceSheet": bs,
        "arAging": extra["arAging"],
        "cashFlow": extra["cashFlow"],
        "cashTrend": extra["cashTrend"],
        "topCustomers": extra["topCustomers"],
        "customerCount": extra["customerCount"],
        "top5Concentration": extra["top5Concentration"],
    }
    return payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", dest="show", action="store_true",
                    help="print the payload instead of writing it")
    args = ap.parse_args()

    payload = build()
    s = payload["summary"]

    if args.show:
        print(json.dumps(payload, indent=2))
    else:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"Wrote {OUT}")

    print(f"{payload['asOf']}: ${s['income']:,.0f} income, "
          f"${s['expenses']:,.0f} expenses, ${s['netIncome']:,.0f} net "
          f"({s['netMargin']*100:.1f}% margin), {payload['elapsed']} months.")


if __name__ == "__main__":
    main()
