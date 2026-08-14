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

# Overhead is split three ways: the cost of employed people, the cost of hired
# people, and everything else. These lists name the top-level P&L rows that are
# people cost — anything not named here falls into non-labour overhead, so a new
# expense account starts out counted without anyone having to remember to add it.
#
# "Taxes paid" is here because in this book it holds only employer payroll taxes
# (FUTA, SUTA, SS, Medicare). If a non-payroll tax account is ever added under it,
# it will need splitting out.
PAYROLL_SECTIONS = ["Payroll expenses", "Taxes paid", "Employee benefits"]
CONTRACTOR_SECTIONS = ["Contract labor"]


def load_contractors():
    """
    Per-contractor spend, if it has been exported.

    QuickBooks' MCP connector has no expenses-by-vendor report, so this cannot be
    pulled automatically — it comes from an "Expenses by Vendor Summary" export
    dropped in as contractors.csv, or a `contractors` list in extra.json.

    QuickBooks puts two or three title rows above the header, so parsing starts
    only after the header row is found. Without that anchor the report's own date
    line ("January 1 - August 9, 2026") parses as a vendor called "January 1 -
    August 9" who was paid $2,026 — a fake contractor, and one that quietly
    shrinks the unaccounted figure this whole card exists to show.
    """
    path = os.path.join(SNAP_DIR, "contractors.csv")
    if not os.path.exists(path):
        return None, None

    import csv as _csv
    with open(path, encoding="utf-8-sig", newline="") as f:
        raw_rows = list(_csv.reader(f))

    # the header is the row naming the amount column
    heads = {"total", "amount", "balance", "total amount", "spend"}
    start = 0
    for i, raw in enumerate(raw_rows):
        if any((c or "").strip().lower() in heads for c in raw):
            start = i + 1
            break

    def money(cells):
        for c in reversed(cells):
            t = c.replace("$", "").replace(",", "").strip()
            if t.startswith("(") and t.endswith(")"):
                t = "-" + t[1:-1]
            # a bare integer with no currency marker is more likely a stray year
            # or a count than an amount, so only accept it when the row is anchored
            if not any(ch in c for ch in "$.,"):
                continue
            try:
                return float(t)
            except ValueError:
                continue
        return None

    rows = []
    for raw in raw_rows[start:]:
        cells = [c.strip() for c in raw if c is not None and c.strip() != ""]
        if len(cells) < 2:
            continue
        name = cells[0]
        if name.lower().startswith("total") or name.lower() in heads:
            continue
        v = money(cells[1:])
        if v:
            rows.append([name, round(v, 2)])
    return (sorted(rows, key=lambda x: -x[1]), "contractors.csv") if rows else (None, None)


def build_contractors(rows_csv, source, account_total):
    """
    The named contractors set against what the Contract labor account says.

    The point of this block is the reconciliation: if the names do not add up to
    the account, the difference is spend belonging to someone who is not on the
    list — which is exactly the "are we missing anyone" question.
    """
    if not rows_csv:
        return None
    named = round(sum(v for _, v in rows_csv), 2)
    gap = round(account_total - named, 2)
    return {
        "rows": rows_csv,
        "count": len(rows_csv),
        "named": named,
        "accountTotal": round(account_total, 2),
        "unaccounted": gap,
        "complete": abs(gap) < 1,
        "source": source,
    }


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


def named_resolvers(rows, names):
    """Resolvers for a named subset of top-level rows, wherever they sit."""
    out = []
    for row in rows:
        name = account_name(row)
        if name not in names or name.startswith("Total for "):
            continue
        if children_of(rows, row["metadata"]["id"]):
            out.append({"total": "Total for " + name,
                        "leaves": leaf_names(rows, row)})
        else:
            out.append({"total": None, "leaves": [name]})
    return out


def named_total(rows, names):
    """Period total for a named subset of top-level rows."""
    total = 0.0
    for row in rows:
        if account_name(row) in names:
            total += amount(row)
    return total


def payroll_detail(rows):
    """The individual lines that make up employed-people cost, largest first."""
    out = []
    for row in rows:
        parent = account_name(row)
        if parent not in PAYROLL_SECTIONS:
            continue
        kids = children_of(rows, row["metadata"]["id"])
        if not kids:
            if amount(row):
                out.append([parent, round(amount(row), 2)])
            continue
        for k in kids:
            name = account_name(k)
            if not name or name.startswith("Total for ") or not amount(k):
                continue
            # one more level down for Taxes paid > Payroll taxes > FUTA/SUTA/…
            grandkids = [g for g in children_of(rows, k["metadata"]["id"])
                         if account_name(g) and not account_name(g).startswith("Total for ")]
            if grandkids and name != parent:
                out.append([name, round(amount(k), 2)])
            else:
                out.append([name, round(amount(k), 2)])
    return sorted(out, key=lambda x: -x[1])


def assemble_overhead(payroll, contractor, m_pay, m_con, m_income, m_overhead,
                      income, total_overhead, detail):
    """
    Shape the overhead block from figures that have already been measured.

    Shared by both ingest paths — the QuickBooks API returns payroll and contractor
    series directly, while the older connector route has to derive them — so the
    arithmetic, the reconciliation and the identity checks live in exactly one
    place regardless of where the numbers came from.
    """
    labor = round(payroll + contractor, 2)
    non_labor = round(total_overhead - labor, 2)

    m_non, m_rate = [], []
    for i in range(12):
        m_non.append(round(m_overhead[i] - m_pay[i] - m_con[i], 2))
        m_rate.append(round(m_overhead[i] / m_income[i], 4) if m_income[i] else 0)

    if abs(sum(m_pay) - payroll) > 1 or abs(sum(m_con) - contractor) > 1:
        sys.exit(f"Labour months do not reconcile: payroll ${sum(m_pay):,.2f} vs "
                 f"${payroll:,.2f}, contractors ${sum(m_con):,.2f} vs "
                 f"${contractor:,.2f}.")

    return {
        "total": round(total_overhead, 2),
        "rate": round(total_overhead / income, 4) if income else 0,
        "perRevenueDollar": round(total_overhead / income, 2) if income else 0,
        "labor": {
            "total": labor,
            "rate": round(labor / income, 4) if income else 0,
            "shareOfOverhead": round(labor / total_overhead, 4) if total_overhead else 0,
            "payroll": payroll,
            "contractor": contractor,
            "payrollShare": round(payroll / labor, 4) if labor else 0,
            "contractorShare": round(contractor / labor, 4) if labor else 0,
            "detail": detail,
        },
        "nonLabor": {
            "total": non_labor,
            "rate": round(non_labor / income, 4) if income else 0,
            "shareOfOverhead": round(non_labor / total_overhead, 4) if total_overhead else 0,
        },
        "monthlyPayroll": m_pay,
        "monthlyContractor": m_con,
        "monthlyNonLabor": m_non,
        "monthlyRate": m_rate,
    }


def build_overhead(rows, month_dicts, m_income, m_overhead, income, total_overhead):
    """Overhead via the older connector route, which must derive the labour series."""
    pay_res = named_resolvers(rows, PAYROLL_SECTIONS)
    con_res = named_resolvers(rows, CONTRACTOR_SECTIONS)
    payroll = round(named_total(rows, PAYROLL_SECTIONS), 2)
    contractor = round(named_total(rows, CONTRACTOR_SECTIONS), 2)

    m_pay = [round(month_total(a, pay_res), 2) for a in month_dicts]
    m_con = [round(month_total(a, con_res), 2) for a in month_dicts]

    return assemble_overhead(
        payroll, contractor, m_pay, m_con, m_income, m_overhead, income,
        total_overhead, payroll_detail(rows) + [["Contract labor", contractor]])


def overhead_notes(overhead, months, complete):
    """
    Things about the overhead picture worth saying in words.

    Only complete months are considered — a month still being written has banked
    part of its revenue but few of its costs, so its rate is meaningless.
    """
    notes = []
    rate = overhead["monthlyRate"][:complete]
    if not rate:
        return notes

    worst = max(range(len(rate)), key=lambda i: rate[i])
    if rate[worst] >= 0.9:
        notes.append(
            f"{months[worst]} overhead ran to {rate[worst] * 100:.0f}% of that "
            f"month's revenue — near break-even, against a "
            f"{overhead['rate'] * 100:.0f}% average.")

    lab = overhead["labor"]
    notes.append(
        f"People are {lab['shareOfOverhead'] * 100:.0f}% of overhead: "
        f"{lab['payrollShare'] * 100:.0f}% payroll, "
        f"{lab['contractorShare'] * 100:.0f}% contractors. Contractors flex with "
        f"the work; payroll does not.")

    c = overhead.get("contractors")
    if c and not c["complete"]:
        if c["unaccounted"] > 0:
            notes.append(
                f"${c['unaccounted']:,.0f} of contract labor is not attributed to "
                f"any of the {c['count']} named contractors — someone is missing "
                f"from the list.")
        else:
            notes.append(
                f"Named contractors total ${-c['unaccounted']:,.0f} more than the "
                f"Contract labor account — the export covers spend booked "
                f"elsewhere too.")
    elif c:
        notes.append(
            f"All ${c['accountTotal']:,.0f} of contract labor is accounted for "
            f"across {c['count']} contractors.")
    return notes


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


def build_from_snapshot(snap):
    """
    Assemble the payload from a QuickBooks API snapshot.

    fetch_qbo.py has already done the parsing and self-checked that the P&L
    balances, so this is assembly plus the same analysis the connector path gets —
    the overhead split, the reconciliations and the notes are shared code, so both
    routes produce a payload the page cannot tell apart.
    """
    pl = snap["pl"]
    t = pl["totals"]
    income = t["income"]
    cogs = t["cogs"]
    total_overhead = round(t["expenses"] + t["other"] + cogs, 2)
    net = t["net"]

    m_income = pl["monthlyIncome"]
    m_expense = pl["monthlyExpenses"]
    m_cogs = pl["monthlyCogs"]
    m_overhead = [round(m_expense[i] + m_cogs[i], 2) for i in range(12)]
    m_net = [round(m_income[i] - m_cogs[i] - m_expense[i], 2) for i in range(12)]

    elapsed = max((i + 1 for i in range(12)
                   if m_income[i] or m_overhead[i]), default=0)
    end = dt.date.fromisoformat(snap["periodEnd"])
    complete = max(0, elapsed - 1) if end.day > 1 else elapsed

    overhead = assemble_overhead(
        pl["payroll"], pl["contractor"], pl["monthlyPayroll"],
        pl["monthlyContractor"], m_income, m_overhead, income, total_overhead,
        pl["laborDetail"])

    con = snap.get("contractors")
    if con and con.get("rows"):
        overhead["contractors"] = con

    prior = snap.get("priorYear") or {}
    prior_same = round(sum((prior.get("monthlyIncome") or [0] * 12)[:complete]), 2)
    current_same = round(sum(m_income[:complete]), 2)

    cust = snap.get("customers") or {}
    gross = round(income - cogs, 2)

    payload = {
        "asOf": end.strftime("%b %-d, %Y"),
        "periodStart": snap["periodStart"],
        "periodEnd": snap["periodEnd"],
        "elapsed": elapsed,
        "completeMonths": complete,
        "note": FISCAL_YEAR_NOTE,
        "source": "QuickBooks API",
        "fetchedAt": snap.get("fetchedAt"),
        "summary": {
            "income": income,
            "cogs": cogs,
            "grossProfit": gross,
            "grossMargin": round(gross / income, 4) if income else 0,
            "expenses": round(t["expenses"] + t["other"], 2),
            "netIncome": net,
            "netMargin": round(net / income, 4) if income else 0,
        },
        "months": MONTHS,
        "monthlyIncome": m_income,
        "monthlyExpenses": m_expense,
        "monthlyCogs": m_cogs,
        "monthlyNet": m_net,
        "monthlyOverhead": m_overhead,
        "overhead": overhead,
        "expenseBreakdown": pl["expenseBreakdown"],
        "priorYear": {
            "label": prior.get("label", ""),
            "income": prior.get("income", 0),
            "netIncome": prior.get("netIncome", 0),
            "compareMonths": complete,
            "compareLabel": f"{MONTHS[0]}–{MONTHS[complete - 1]}" if complete else "",
            "incomeSamePeriod": prior_same,
            "incomeThisPeriod": current_same,
            "incomeChange": round(current_same - prior_same, 2),
            "incomeChangePct": round((current_same - prior_same) / prior_same, 4)
            if prior_same else 0,
        },
        "balanceSheet": snap["balanceSheet"],
        "arAging": snap["arAging"],
        "cashFlow": snap["cashFlow"],
        "cashTrend": snap.get("cashTrend") or [],
        "topCustomers": cust.get("rows", []),
        "customerCount": cust.get("count", 0),
        "top5Concentration": cust.get("top5Concentration", 0),
        "dataNotes": overhead_notes(overhead, MONTHS, complete)
                     + snapshot_notes(pl, income),
    }
    return payload


def snapshot_notes(pl, income):
    """The same caveats as the connector path, read off the snapshot instead."""
    notes = []
    by = dict((k, v) for k, v in pl["expenseBreakdown"])
    labor = by.get("Contract labor", 0)
    cogs = pl["totals"]["cogs"]
    if labor > cogs:
        notes.append(
            f"Gross margin reads high: ${labor:,.0f} of contract labor sits in "
            f"overhead rather than cost of sales, so only ${cogs:,.0f} counts "
            f"against revenue.")
    holding = by.get("Ask My Client", 0)
    if holding:
        notes.append(
            f"${holding:,.0f} is still parked in \"Ask My Client\" awaiting "
            f"categorisation, so the expense split below will shift.")
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
    month_dicts = [{} for _ in range(12)]
    elapsed = 0

    for key in sorted(monthly):
        start = key.split(" - ")[0]
        idx = int(start[5:7]) - 1
        block = monthly[key]
        m_income[idx] = round(block.get("totalIncome", 0) or 0, 2)
        # operating expenses + other expenses, both shaped from the tree
        acct = block.get("expenseAccounts", {}) or {}
        month_dicts[idx] = acct
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

    # Everything it costs to run the studio — operating expenses, the below-the-line
    # vehicle costs, and cost of sales. Anything that is not revenue and not an
    # owner draw belongs in here.
    m_overhead = [round(m_expense[i] + m_cogs[i], 2) for i in range(12)]
    overhead = build_overhead(rows, month_dicts, m_income, m_overhead,
                              income, expenses + other_exp + cogs)

    csv_rows, csv_source = load_contractors()
    contractors = build_contractors(csv_rows, csv_source,
                                    overhead["labor"]["contractor"])
    if contractors:
        overhead["contractors"] = contractors

    # Date the books were read, not the date this ran — a stale snapshot rebuilt
    # today must not claim to be today's numbers.
    as_of = dt.date.fromisoformat(pl["periodEnd"]) if pl.get("periodEnd") else today

    bs = extra["balanceSheet"]
    payload = {
        "asOf": as_of.strftime("%b %-d, %Y"),
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
        "completeMonths": complete,
        "months": MONTHS,
        "monthlyIncome": m_income,
        "monthlyExpenses": m_expense,
        "monthlyCogs": m_cogs,
        "monthlyNet": m_net,
        "monthlyOverhead": m_overhead,
        "overhead": overhead,
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
        "dataNotes": overhead_notes(overhead, MONTHS, complete)
                     + data_notes(rows, income),
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
    ap.add_argument("--source", choices=["auto", "api", "mcp"], default="auto",
                    help="where to read from; auto prefers a QuickBooks API snapshot")
    args = ap.parse_args()

    # The API snapshot wins when present: it is the automated path, and it carries
    # per-contractor detail the connector route cannot produce at all.
    snap_path = os.path.join(SNAP_DIR, "qbo_snapshot.json")
    use_api = args.source == "api" or (args.source == "auto"
                                       and os.path.exists(snap_path))
    if use_api:
        if not os.path.exists(snap_path):
            sys.exit(f"No API snapshot at {snap_path}\n"
                     "  Run: python3 scripts/fetch_qbo.py")
        with open(snap_path, encoding="utf-8") as f:
            payload = build_from_snapshot(json.load(f))
    else:
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
