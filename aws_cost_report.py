#!/usr/bin/env python3
"""
AWS Cost Summary Report
========================
Output format:

  AWS Cost Summary
  Kratos Production
  Account ID: 123456789012

    • July 2026       : $9,867.66
    • August 2026     : $7,526.80
    • September 2026  : $6,587.85
    • October 2026    : $2,768.65 (Till Date)

    Total Cost : $26,750.96

Requirements:
  - Python 3.8+
  - boto3 >= 1.34.0
  - AWS credentials via: aws configure  OR  env vars  OR  IAM Role

IAM permissions needed:
  - ce:GetCostAndUsage
  - sts:GetCallerIdentity
"""

import sys
import json
import argparse
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional, List, Tuple

try:
    import boto3
    from botocore.exceptions import (
        ClientError,
        NoCredentialsError,
        ProfileNotFound,
    )
except ImportError:
    print("[ERROR] boto3 is not installed. Run: pip install -r requirements.txt")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GRANULARITY_MONTHLY = "MONTHLY"
GRANULARITY_DAILY   = "DAILY"
COST_METRIC         = "UnblendedCost"   # change to "BlendedCost" if preferred
CURRENCY            = "USD"

SEPARATOR = "═" * 72
THIN_SEP  = "─" * 72

# ---------------------------------------------------------------------------
# Charge-type filter  (mirrors "Charge type → Usage" in Cost Explorer UI)
# ---------------------------------------------------------------------------
# AWS RECORD_TYPE values:
#   Usage                  — actual service consumption  ← INCLUDED
#   Bundled Discount       — negative discount applied to usage ← INCLUDED
#   Tax                    — sales/VAT tax               ← EXCLUDED
#   Credit                 — AWS promotional credits     ← EXCLUDED
#   Refund                 — billing refunds             ← EXCLUDED
#   Fee                    — support plan fees           ← EXCLUDED
#   RIFee                  — Reserved Instance upfront   ← EXCLUDED
#   SavingsPlanCoveredUsage— SP-covered compute          ← EXCLUDED
#   SavingsPlanNegation    — SP discount negation        ← EXCLUDED
#
# "Bundled Discount" is a negative line item that the console nets off against
# Usage automatically. Including it here ensures our totals match the console.
INCLUDED_CHARGE_TYPES = ["Usage", "Bundled Discount"]

CHARGE_TYPE_FILTER = {
    "Dimensions": {
        "Key": "RECORD_TYPE",
        "Values": INCLUDED_CHARGE_TYPES,
        "MatchOptions": ["EQUALS"],
    }
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ============================================================================
# Utility helpers
# ============================================================================

def _fmt_usd(amount: float) -> str:
    """Format as $1,234.56 — matches the template style."""
    return f"${amount:,.2f}"


def _fmt_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _month_label(year: int, month: int) -> str:
    return date(year, month, 1).strftime("%B %Y")


def _first_of_month(year: int, month: int) -> date:
    return date(year, month, 1)


def _next_first_of_month(year: int, month: int) -> date:
    """First day of the month AFTER (year, month) — used as CE exclusive end."""
    if month == 12:
        return date(year + 1, 1, 1)
    return date(year, month + 1, 1)


def _subtract_months(ref: date, n: int) -> date:
    """First day of the month that is n months before ref's month."""
    m, y = ref.month - n, ref.year
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


# ============================================================================
# AWS session / identity
# ============================================================================

def build_session(profile: Optional[str] = None) -> boto3.Session:
    try:
        kwargs = {"region_name": "us-east-1"}
        if profile:
            kwargs["profile_name"] = profile
        return boto3.Session(**kwargs)
    except ProfileNotFound:
        print(f"\n[ERROR] AWS profile '{profile}' not found.")
        print(f"  Run: aws configure --profile {profile}\n")
        sys.exit(1)


def get_account_identity(session: boto3.Session) -> dict:
    try:
        return session.client("sts", region_name="us-east-1").get_caller_identity()
    except NoCredentialsError:
        print("\n[ERROR] No AWS credentials found.")
        print("  Option 1: Run `aws configure`")
        print("  Option 2: Export AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY")
        print("  Option 3: Attach an IAM Role to this instance\n")
        sys.exit(1)
    except ClientError as exc:
        print(f"\n[ERROR] STS: {exc.response['Error']['Message']}\n")
        sys.exit(1)


def build_ce_client(session: boto3.Session):
    return session.client("ce", region_name="us-east-1")


# ============================================================================
# Cost Explorer query  (single entry point — filter always applied)
# ============================================================================

def get_cost_and_usage(
    ce_client,
    start: str,
    end: str,
    granularity: str,
    group_by_service: bool = False,
) -> list:
    """
    Fetch cost & usage. CHARGE_TYPE_FILTER is always injected so only
    'Usage' records are returned (no Credits, Tax, Refunds, Fees).
    Handles CE pagination automatically.
    """
    params = {
        "TimePeriod": {"Start": start, "End": end},
        "Granularity": granularity,
        "Metrics": [COST_METRIC],
        "Filter": CHARGE_TYPE_FILTER,
    }
    if group_by_service:
        params["GroupBy"] = [{"Type": "DIMENSION", "Key": "SERVICE"}]

    results, next_token = [], None
    while True:
        if next_token:
            params["NextPageToken"] = next_token
        try:
            resp = ce_client.get_cost_and_usage(**params)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            msg  = exc.response["Error"]["Message"]
            if code == "AccessDeniedException":
                print(f"\n[ERROR] Access Denied: {msg}")
                print("  Attach the ce:GetCostAndUsage IAM permission to your user/role.\n")
                sys.exit(1)
            raise
        results.extend(resp.get("ResultsByTime", []))
        next_token = resp.get("NextPageToken")
        if not next_token:
            break
    return results


# ============================================================================
# Data-fetching helpers  (return plain numbers, no printing)
# ============================================================================

def fetch_month_total(ce_client, year: int, month: int, today: date) -> float:
    """
    Return total cost for a given (year, month).
    For the current month uses today as the exclusive end date.
    """
    start = _fmt_date(_first_of_month(year, month))
    end   = _fmt_date(today + timedelta(days=1)) \
            if (year == today.year and month == today.month) \
            else _fmt_date(_next_first_of_month(year, month))

    results = get_cost_and_usage(ce_client, start, end, GRANULARITY_MONTHLY)
    return sum(
        float(r["Total"].get(COST_METRIC, {}).get("Amount", 0))
        for r in results
    )


def fetch_month_services(ce_client, year: int, month: int, today: date) -> dict:
    """
    Return {service_name: cost} for a given (year, month).
    """
    start = _fmt_date(_first_of_month(year, month))
    end   = _fmt_date(today + timedelta(days=1)) \
            if (year == today.year and month == today.month) \
            else _fmt_date(_next_first_of_month(year, month))

    results = get_cost_and_usage(ce_client, start, end, GRANULARITY_MONTHLY,
                                 group_by_service=True)
    services: dict = {}
    for r in results:
        for group in r.get("Groups", []):
            svc = group["Keys"][0]
            amt = float(group["Metrics"][COST_METRIC]["Amount"])
            services[svc] = services.get(svc, 0.0) + amt
    return services


def fetch_daily(ce_client, today: date) -> list:
    """
    Return [(date_str, amount), ...] for every day in the current month up to today.
    """
    start = _fmt_date(_first_of_month(today.year, today.month))
    end   = _fmt_date(today + timedelta(days=1))
    results = get_cost_and_usage(ce_client, start, end, GRANULARITY_DAILY)
    return [
        (r["TimePeriod"]["Start"],
         float(r["Total"].get(COST_METRIC, {}).get("Amount", 0)))
        for r in results
    ]


# ============================================================================
# Months to cover  (past N complete months + current MTD month)
# ============================================================================

def months_to_show(today: date, num_past_months: int) -> List[Tuple[int, int, bool]]:
    """
    Returns a list of (year, month, is_mtd) from oldest → newest.
    is_mtd = True only for the current calendar month.
    """
    months = []
    for i in range(num_past_months, 0, -1):
        anchor = _subtract_months(today, i)
        months.append((anchor.year, anchor.month, False))
    months.append((today.year, today.month, True))
    return months


# ============================================================================
# Output printers
# ============================================================================

def print_header(account_name: str, account_id: str):
    print()
    print("AWS Cost Summary")
    if account_name:
        print(account_name)
    print(f"Account ID: {account_id}")
    print()


def print_cost_summary(
    ce_client,
    today: date,
    num_past_months: int,
) -> float:
    """
    Prints the compact month-by-month summary block:

      • July 2026         : $9,867.66
      • October 2026      : $2,768.65 (Till Date)

      Total Cost : $26,750.96

    Returns the grand total.
    """
    month_list = months_to_show(today, num_past_months)
    rows: List[Tuple[str, float, bool]] = []

    for year, month, is_mtd in month_list:
        total = fetch_month_total(ce_client, year, month, today)
        rows.append((_month_label(year, month), total, is_mtd))

    grand_total = sum(amt for _, amt, _ in rows)

    label_width = max(len(label) for label, _, _ in rows) + 2

    for label, amt, is_mtd in rows:
        till = " (Till Date)" if is_mtd else ""
        print(f"  • {label:<{label_width}}: {_fmt_usd(amt)}{till}")

    print()
    print(f"  Total Cost : {_fmt_usd(grand_total)}")
    print()

    return grand_total


def print_service_breakdown(
    ce_client,
    today: date,
    num_past_months: int,
):
    """
    Prints a per-service table for every month in scope.

      ── July 2026 ──
        Amazon EC2          :  $5,100.00
        Amazon RDS          :  $3,200.00
        ...
        ─────────────────────────────
        Total               :  $9,867.66
    """
    month_list = months_to_show(today, num_past_months)

    print()
    print(f"  {'── Per-Service Breakdown ──':^68}")
    print()

    for year, month, is_mtd in month_list:
        label  = _month_label(year, month)
        suffix = " (Till Date)" if is_mtd else ""
        services = fetch_month_services(ce_client, year, month, today)
        month_total = sum(services.values())

        print(f"  ── {label}{suffix} ──")
        if not services:
            print("    No data available.")
        else:
            svc_width = max((len(s) for s in services), default=30) + 2
            for svc, amt in sorted(services.items(), key=lambda x: x[1], reverse=True):
                if amt > 0.0001:
                    print(f"    {svc:<{svc_width}}:  {_fmt_usd(amt)}")
            print(f"    {THIN_SEP[:svc_width + 16]}")
            print(f"    {'Total':<{svc_width}}:  {_fmt_usd(month_total)}")
        print()


def print_daily_trend(ce_client, today: date):
    """
    Prints a day-by-day spend table with a bar chart for the current month.
    """
    daily = fetch_daily(ce_client, today)
    label = _month_label(today.year, today.month)

    print()
    print(f"  ── Daily Spend Trend — {label} (Till Date) ──")
    print()
    print(f"    {'Date':<14} {'Amount':>12}   Bar")
    print(f"    {THIN_SEP[:50]}")

    max_amt = max((amt for _, amt in daily), default=0.01)
    max_amt = max(max_amt, 0.01)
    running = 0.0

    for day_str, amt in daily:
        running += amt
        bar = "█" * int((amt / max_amt) * 30)
        print(f"    {day_str:<14} {_fmt_usd(amt):>12}   {bar}")

    print(f"    {THIN_SEP[:50]}")
    print(f"    {'MTD Total':<14} {_fmt_usd(running):>12}")
    print()


# ============================================================================
# JSON export
# ============================================================================

def export_json(ce_client, today: date, num_past_months: int, output_file: str):
    """Write the full report data to a JSON file."""
    month_list = months_to_show(today, num_past_months)
    report = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "cost_metric": COST_METRIC,
        "charge_types_included": INCLUDED_CHARGE_TYPES,
        "months": [],
    }

    for year, month, is_mtd in month_list:
        services  = fetch_month_services(ce_client, year, month, today)
        total     = sum(services.values())
        report["months"].append({
            "label":    _month_label(year, month),
            "is_mtd":   is_mtd,
            "services": services,
            "total":    round(total, 4),
        })

    with open(output_file, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(f"\n  [OK] JSON exported → {output_file}")


# ============================================================================
# CLI
# ============================================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="AWS Cost Summary — clean month-by-month report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Default: last 3 complete months + current MTD
  python aws_cost_report.py

  # Last 6 months + current MTD
  python aws_cost_report.py --months 6

  # With daily breakdown for current month
  python aws_cost_report.py --months 3 --daily

  # Use a named AWS profile
  python aws_cost_report.py --profile production

  # Set custom account label shown in the header
  python aws_cost_report.py --account-name "Kratos Production"

  # Export to JSON
  python aws_cost_report.py --export cost_oct2026.json

  # Skip per-service detail (fast summary only)
  python aws_cost_report.py --no-service-detail
        """,
    )
    parser.add_argument(
        "--profile", "-p",
        default=None,
        help="AWS CLI profile name",
    )
    parser.add_argument(
        "--account-name", "-n",
        default="",
        help="Friendly account label shown in the report header",
    )
    parser.add_argument(
        "--months", "-m",
        type=int,
        default=3,
        help="Number of past complete months to include (default: 3)",
    )
    parser.add_argument(
        "--daily", "-d",
        action="store_true",
        help="Show daily spend trend for the current month",
    )
    parser.add_argument(
        "--no-service-detail",
        action="store_true",
        default=True,
        help="Skip per-service breakdown (faster, summary only)",
    )
    parser.add_argument(
        "--service-detail",
        action="store_true",
        default=False,
        help="Show per-service breakdown for each month",
    )
    parser.add_argument(
        "--export", "-e",
        metavar="FILE",
        default=None,
        help="Export full report to a JSON file",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


# ============================================================================
# Entry point
# ============================================================================

def main():
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        boto3.set_stream_logger(level=logging.DEBUG)

    today = date.today()

    session  = build_session(profile=args.profile)
    identity = get_account_identity(session)
    account_id   = identity.get("Account", "N/A")
    account_name = args.account_name  # empty string if not provided — header handles it

    ce = build_ce_client(session)

    # ── Header ──────────────────────────────────────────────────────────────
    print_header(account_name, account_id)

    # ── Month-by-month summary ───────────────────────────────────────────────
    print_cost_summary(ce, today, args.months)

    # ── Per-service breakdown (opt-in via --service-detail) ─────────────────
    if args.service_detail:
        print_service_breakdown(ce, today, args.months)

    # ── Daily trend ──────────────────────────────────────────────────────────
    if args.daily:
        print_daily_trend(ce, today)

    # ── JSON export ──────────────────────────────────────────────────────────
    if args.export:
        export_json(ce, today, args.months, args.export)


if __name__ == "__main__":
    main()
