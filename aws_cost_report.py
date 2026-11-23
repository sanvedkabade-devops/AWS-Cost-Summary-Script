#!/usr/bin/env python3
"""
AWS Cost Summary Report
========================
Output format (unchanged):

  AWS Cost Summary
  Kratos Production
  Account ID: 123456789012

    • July 2026       : $9,867.66
    • August 2026     : $7,526.80
    • September 2026  : $6,587.85
    • October 2026    : $2,768.65 (Till Date)

    Total Cost : $26,750.96

Highlights:
  - One Cost Explorer call per grouping (not one per month).
  - Completed months are cached locally, so repeat runs only fetch the
    current month.
  - Multiple profiles (organisations / accounts) per run, queried in parallel.
  - Optional per-service and per-linked-account breakdowns.
  - text, markdown, csv and json output.

Requirements:
  - Python 3.8+
  - boto3 >= 1.34.0
  - AWS credentials via: aws configure / aws sso login / env vars / IAM role

IAM permissions needed:
  - ce:GetCostAndUsage
  - ce:GetDimensionValues   (only for --by-account, to show account names)
  - sts:GetCallerIdentity
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
        NoCredentialsError,
        ProfileNotFound,
        SSOTokenLoadError,
        TokenRetrievalError,
        UnauthorizedSSOTokenError,
    )
except ImportError:
    print("[ERROR] boto3 is not installed. Run: pip install -r requirements.txt",
          file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GRANULARITY_MONTHLY = "MONTHLY"
GRANULARITY_DAILY = "DAILY"

METRICS = [
    "UnblendedCost",
    "BlendedCost",
    "AmortizedCost",
    "NetUnblendedCost",
    "NetAmortizedCost",
]
DEFAULT_METRIC = "UnblendedCost"

# Mirrors "Charge type -> Usage" in the Cost Explorer console. "Bundled
# Discount" is a negative line the console nets against Usage.
# Other RECORD_TYPE values (Tax, Credit, Refund, Fee, RIFee, SavingsPlan*)
# are excluded by default. Override with --charge-types (or "all").
DEFAULT_CHARGE_TYPES = ("Usage", "Bundled Discount")

DIM_SERVICE = "SERVICE"
DIM_ACCOUNT = "LINKED_ACCOUNT"

MAX_MONTHS = 12          # Cost Explorer default look-back
DEFAULT_CACHE_TTL_HOURS = 24
CACHE_DIR = Path(os.environ.get("AWS_COST_REPORT_CACHE",
                                Path.home() / ".cache" / "aws-cost-report"))

SEPARATOR = "═" * 72
THIN_SEP = "─" * 72

logger = logging.getLogger("aws_cost_report")


class CostReportError(Exception):
    """A user-facing error for a single profile (never a traceback)."""


# ============================================================================
# Date helpers (all UTC, because Cost Explorer works in UTC)
# ============================================================================

def utc_today() -> date:
    return datetime.now(tz=timezone.utc).date()


def _fmt_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _month_label(year: int, month: int) -> str:
    return date(year, month, 1).strftime("%B %Y")


def _month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _subtract_months(ref: date, n: int) -> date:
    """First day of the month that is n months before ref's month."""
    total = ref.year * 12 + (ref.month - 1) - n
    return date(total // 12, total % 12 + 1, 1)


def months_to_show(today: date, num_past_months: int) -> List[Tuple[int, int, bool]]:
    """(year, month, is_mtd) oldest -> newest: N complete months + current."""
    months = []
    for i in range(num_past_months, 0, -1):
        anchor = _subtract_months(today, i)
        months.append((anchor.year, anchor.month, False))
    months.append((today.year, today.month, True))
    return months


def _fmt_usd(amount: float) -> str:
    """$1,234.56 (negative: -$1,234.56)."""
    if round(amount, 2) == 0:
        amount = 0.0
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


# ============================================================================
# Query definition
# ============================================================================

@dataclass(frozen=True)
class CostQuery:
    metric: str = DEFAULT_METRIC
    charge_types: Tuple[str, ...] = DEFAULT_CHARGE_TYPES   # empty = no filter

    def ce_filter(self) -> Optional[dict]:
        if not self.charge_types:
            return None
        return {
            "Dimensions": {
                "Key": "RECORD_TYPE",
                "Values": list(self.charge_types),
                "MatchOptions": ["EQUALS"],
            }
        }

    def slot(self, group_dim: Optional[str]) -> str:
        """Cache key: everything that changes the numbers."""
        return "|".join([self.metric, ",".join(self.charge_types) or "all",
                         group_dim or "none"])


# ============================================================================
# AWS session / identity
# ============================================================================

def build_session(profile: Optional[str]) -> boto3.Session:
    try:
        kwargs: Dict[str, Any] = {"region_name": "us-east-1"}
        if profile:
            kwargs["profile_name"] = profile
        return boto3.Session(**kwargs)
    except ProfileNotFound:
        raise CostReportError(
            f"AWS profile '{profile}' not found. "
            f"Run: aws configure --profile {profile}")


def _sso_hint(profile: Optional[str]) -> str:
    flag = f" --profile {profile}" if profile else ""
    return f"AWS SSO session expired or missing. Run: aws sso login{flag}"


def get_account_id(session: boto3.Session, profile: Optional[str]) -> str:
    try:
        return session.client("sts", region_name="us-east-1") \
                      .get_caller_identity()["Account"]
    except NoCredentialsError:
        raise CostReportError(
            "No AWS credentials found. Use `aws configure`, `aws sso login`, "
            "environment variables, or an IAM role.")
    except (TokenRetrievalError, UnauthorizedSSOTokenError, SSOTokenLoadError):
        raise CostReportError(_sso_hint(profile))
    except ClientError as exc:
        err = exc.response["Error"]
        if err.get("Code") in ("ExpiredToken", "ExpiredTokenException"):
            raise CostReportError(
                "Credentials expired. Refresh them (e.g. `aws sso login`).")
        raise CostReportError(f"STS: {err.get('Message', err.get('Code'))}")
    except BotoCoreError as exc:
        raise CostReportError(f"AWS SDK error: {exc}")


def build_ce_client(session: boto3.Session):
    cfg = Config(retries={"max_attempts": 8, "mode": "adaptive"})
    return session.client("ce", region_name="us-east-1", config=cfg)


# ============================================================================
# Local cache for completed months
# ============================================================================

class CostCache:
    """
    Per-account JSON file: {slot: {"YYYY-MM": {"fetched_at": iso, "values": {}}}}.
    Only completed months are cached; the current month is always live.
    """

    def __init__(self, account_id: str, ttl_hours: float,
                 enabled: bool = True, refresh: bool = False,
                 directory: Path = CACHE_DIR):
        self.enabled = enabled
        self.refresh = refresh
        self.ttl = timedelta(hours=ttl_hours)
        self.path = Path(directory) / f"{account_id}.json"
        self._data: Optional[dict] = None
        self._dirty = False

    def _load(self) -> dict:
        if self._data is None:
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def get(self, slot: str, month_key: str) -> Optional[Dict[str, float]]:
        if not self.enabled or self.refresh:
            return None
        entry = self._load().get(slot, {}).get(month_key)
        if not entry:
            return None
        try:
            fetched = datetime.fromisoformat(entry["fetched_at"])
            if datetime.now(tz=timezone.utc) - fetched > self.ttl:
                return None
            return {k: float(v) for k, v in entry["values"].items()}
        except (KeyError, ValueError, TypeError, AttributeError):
            return None

    def put(self, slot: str, month_key: str, values: Dict[str, float]) -> None:
        if not self.enabled:
            return
        self._load().setdefault(slot, {})[month_key] = {
            "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
            "values": values,
        }
        self._dirty = True

    def save(self) -> None:
        if not (self.enabled and self._dirty):
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh)
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError as exc:
            logger.debug("Could not write cache %s: %s", self.path, exc)


# ============================================================================
# Cost Explorer access
# ============================================================================

def _ce_paginate(method, result_key: str, **params) -> list:
    """Call a paginated CE API and return the concatenated result lists."""
    out: list = []
    while True:
        logger.debug("CE call %s %s", method.__name__, {
            k: v for k, v in params.items() if k != "NextPageToken"})
        try:
            resp = method(**params)
        except ClientError as exc:
            err = exc.response["Error"]
            code, msg = err.get("Code", ""), err.get("Message", "")
            if code == "AccessDeniedException":
                raise CostReportError(
                    f"Access denied: {msg}. Attach ce:GetCostAndUsage "
                    f"(and ce:GetDimensionValues for --by-account).")
            raise CostReportError(f"Cost Explorer: {code}: {msg}")
        except (TokenRetrievalError, UnauthorizedSSOTokenError, SSOTokenLoadError):
            raise CostReportError("AWS SSO session expired. Run: aws sso login")
        except BotoCoreError as exc:
            raise CostReportError(f"AWS SDK error: {exc}")
        out.extend(resp.get(result_key, []))
        token = resp.get("NextPageToken")
        if not token:
            return out
        params["NextPageToken"] = token


def _query_monthly(ce, start: str, end: str, query: CostQuery,
                   group_dim: Optional[str]) -> Dict[Tuple[int, int], Dict[str, float]]:
    """ONE ranged MONTHLY call -> {(year, month): {group: amount}}."""
    params: Dict[str, Any] = {
        "TimePeriod": {"Start": start, "End": end},
        "Granularity": GRANULARITY_MONTHLY,
        "Metrics": [query.metric],
    }
    flt = query.ce_filter()
    if flt:
        params["Filter"] = flt
    if group_dim:
        params["GroupBy"] = [{"Type": "DIMENSION", "Key": group_dim}]

    out: Dict[Tuple[int, int], Dict[str, float]] = {}
    for r in _ce_paginate(ce.get_cost_and_usage, "ResultsByTime", **params):
        s = r["TimePeriod"]["Start"]
        bucket = out.setdefault((int(s[:4]), int(s[5:7])), {})
        if group_dim:
            for g in r.get("Groups", []):
                key = g["Keys"][0]
                bucket[key] = bucket.get(key, 0.0) + \
                    float(g["Metrics"][query.metric]["Amount"])
        else:
            amt = float(r.get("Total", {}).get(query.metric, {}).get("Amount", 0))
            bucket["Total"] = bucket.get("Total", 0.0) + amt
    return out


def fetch_monthly(ce, months: List[Tuple[int, int, bool]], today: date,
                  query: CostQuery, group_dim: Optional[str],
                  cache: CostCache) -> Dict[Tuple[int, int], Dict[str, float]]:
    """
    Return {(year, month): {group: amount}} for every month in `months`.
    Completed months come from the cache when possible; everything else is
    fetched with a single ranged call (oldest missing month -> today).
    """
    slot = query.slot(group_dim)
    result: Dict[Tuple[int, int], Dict[str, float]] = {}
    needed: List[Tuple[int, int, bool]] = []

    for year, month, is_mtd in months:
        cached = None if is_mtd else cache.get(slot, _month_key(year, month))
        if cached is not None:
            result[(year, month)] = cached
        else:
            needed.append((year, month, is_mtd))

    if needed:  # the current month is always here
        y0, m0, _ = needed[0]
        fetched = _query_monthly(
            ce, _fmt_date(date(y0, m0, 1)), _fmt_date(today + timedelta(days=1)),
            query, group_dim)
        for year, month, is_mtd in needed:
            values = fetched.get((year, month), {})
            result[(year, month)] = values
            if not is_mtd:
                cache.put(slot, _month_key(year, month), values)
        cache.save()
    return result


def fetch_daily(ce, today: date, query: CostQuery) -> List[Tuple[str, float]]:
    """[(YYYY-MM-DD, amount)] for each day of the current month so far."""
    params: Dict[str, Any] = {
        "TimePeriod": {"Start": _fmt_date(date(today.year, today.month, 1)),
                       "End": _fmt_date(today + timedelta(days=1))},
        "Granularity": GRANULARITY_DAILY,
        "Metrics": [query.metric],
    }
    flt = query.ce_filter()
    if flt:
        params["Filter"] = flt
    rows = _ce_paginate(ce.get_cost_and_usage, "ResultsByTime", **params)
    return [(r["TimePeriod"]["Start"],
             float(r["Total"].get(query.metric, {}).get("Amount", 0)))
            for r in rows]


def fetch_account_names(ce, months, today: date) -> Dict[str, str]:
    """{account_id: friendly name}. Best effort; empty on any failure."""
    y0, m0, _ = months[0]
    try:
        values = _ce_paginate(
            ce.get_dimension_values, "DimensionValues",
            TimePeriod={"Start": _fmt_date(date(y0, m0, 1)),
                        "End": _fmt_date(today + timedelta(days=1))},
            Dimension=DIM_ACCOUNT, Context="COST_AND_USAGE")
    except CostReportError as exc:
        logger.debug("Account names unavailable: %s", exc)
        return {}
    return {v["Value"]: v.get("Attributes", {}).get("description", "")
            for v in values}


# ============================================================================
# Report model
# ============================================================================

@dataclass
class MonthResult:
    year: int
    month: int
    is_mtd: bool
    total: float
    services: Dict[str, float] = field(default_factory=dict)
    accounts: Dict[str, float] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return _month_label(self.year, self.month)


@dataclass
class Report:
    profile: Optional[str]
    account_id: str
    account_name: str
    metric: str
    charge_types: Tuple[str, ...]
    months: List[MonthResult]
    account_names: Dict[str, str] = field(default_factory=dict)
    daily: Optional[List[Tuple[str, float]]] = None

    @property
    def total(self) -> float:
        return sum(m.total for m in self.months)


def generate_report(profile: Optional[str], name: str, opts: argparse.Namespace,
                    today: date) -> Report:
    session = build_session(profile)
    account_id = get_account_id(session, profile)
    ce = build_ce_client(session)
    query = CostQuery(opts.metric, tuple(opts.charge_types))
    cache = CostCache(account_id, opts.cache_ttl,
                      enabled=not opts.no_cache, refresh=opts.refresh)
    months = months_to_show(today, opts.months)

    services = accounts = None
    if opts.service_detail:
        services = fetch_monthly(ce, months, today, query, DIM_SERVICE, cache)
    if opts.by_account:
        accounts = fetch_monthly(ce, months, today, query, DIM_ACCOUNT, cache)
    totals_src = services if services is not None else accounts
    if totals_src is None:
        totals_src = fetch_monthly(ce, months, today, query, None, cache)

    results = []
    for year, month, is_mtd in months:
        key = (year, month)
        results.append(MonthResult(
            year, month, is_mtd,
            total=sum(totals_src.get(key, {}).values()),
            services=services.get(key, {}) if services is not None else {},
            accounts=accounts.get(key, {}) if accounts is not None else {},
        ))

    return Report(
        profile=profile, account_id=account_id, account_name=name,
        metric=query.metric, charge_types=query.charge_types, months=results,
        account_names=fetch_account_names(ce, months, today) if accounts is not None else {},
        daily=fetch_daily(ce, today, query) if opts.daily else None,
    )


# ============================================================================
# Renderers
# ============================================================================

def _breakdown_lines(title: str, report: Report, attr: str,
                     names: Optional[Dict[str, str]] = None) -> List[str]:
    lines = ["", f"  {title:^68}", ""]
    for m in report.months:
        suffix = " (Till Date)" if m.is_mtd else ""
        data: Dict[str, float] = getattr(m, attr)
        lines.append(f"  ── {m.label}{suffix} ──")
        if not data:
            lines.append("    No data available.")
        else:
            def disp(k: str) -> str:
                n = (names or {}).get(k)
                return f"{n} ({k})" if n else k
            width = max(len(disp(k)) for k in data) + 2
            for k, amt in sorted(data.items(), key=lambda x: x[1], reverse=True):
                if amt > 0.0001:
                    lines.append(f"    {disp(k):<{width}}:  {_fmt_usd(amt)}")
            lines.append(f"    {THIN_SEP[:width + 16]}")
            lines.append(f"    {'Total':<{width}}:  {_fmt_usd(sum(data.values()))}")
        lines.append("")
    return lines


def render_text(report: Report, opts: argparse.Namespace) -> str:
    out = ["", "AWS Cost Summary"]
    if report.account_name:
        out.append(report.account_name)
    out += [f"Account ID: {report.account_id}", ""]

    width = max(len(m.label) for m in report.months) + 2
    for m in report.months:
        till = " (Till Date)" if m.is_mtd else ""
        out.append(f"  • {m.label:<{width}}: {_fmt_usd(m.total)}{till}")
    out += ["", f"  Total Cost : {_fmt_usd(report.total)}", ""]

    if opts.service_detail:
        out += _breakdown_lines("── Per-Service Breakdown ──", report, "services")
    if opts.by_account:
        out += _breakdown_lines("── Per-Account Breakdown ──", report, "accounts",
                                report.account_names)
    if report.daily is not None:
        out += _daily_lines(report)
    return "\n".join(out)


def _daily_lines(report: Report) -> List[str]:
    daily = report.daily or []
    label = report.months[-1].label
    lines = ["", f"  ── Daily Spend Trend — {label} (Till Date) ──", "",
             f"    {'Date':<14} {'Amount':>12}   Bar", f"    {THIN_SEP[:50]}"]
    max_amt = max(max((a for _, a in daily), default=0.01), 0.01)
    running = 0.0
    for day, amt in daily:
        running += amt
        bar = "█" * max(0, int((amt / max_amt) * 30))
        lines.append(f"    {day:<14} {_fmt_usd(amt):>12}   {bar}")
    lines += [f"    {THIN_SEP[:50]}", f"    {'MTD Total':<14} {_fmt_usd(running):>12}", ""]
    return lines


def render_markdown(report: Report, opts: argparse.Namespace) -> str:
    title = report.account_name or report.profile or "AWS account"
    out = [f"## {title} (Account ID: {report.account_id})", "",
           "| Month | Cost |", "|---|---:|"]
    for m in report.months:
        till = " (Till Date)" if m.is_mtd else ""
        out.append(f"| {m.label}{till} | {_fmt_usd(m.total)} |")
    out += [f"| **Total** | **{_fmt_usd(report.total)}** |", ""]

    def table(heading: str, attr: str, names: Optional[Dict[str, str]] = None):
        out.append(f"### {heading}")
        for m in report.months:
            data = getattr(m, attr)
            out += ["", f"**{m.label}{' (Till Date)' if m.is_mtd else ''}**", "",
                    "| Name | Cost |", "|---|---:|"]
            for k, amt in sorted(data.items(), key=lambda x: x[1], reverse=True):
                if amt > 0.0001:
                    n = (names or {}).get(k)
                    out.append(f"| {f'{n} ({k})' if n else k} | {_fmt_usd(amt)} |")
        out.append("")

    if opts.service_detail:
        table("Per-Service Breakdown", "services")
    if opts.by_account:
        table("Per-Account Breakdown", "accounts", report.account_names)
    if report.daily is not None:
        out += ["### Daily Spend", "", "| Date | Cost |", "|---|---:|"]
        out += [f"| {d} | {_fmt_usd(a)} |" for d, a in report.daily]
        out.append("")
    return "\n".join(out)


CSV_HEADER = ["profile", "account_id", "account_name", "month", "till_date",
              "type", "name", "amount"]


def csv_rows(report: Report) -> List[List[Any]]:
    base = [report.profile or "", report.account_id, report.account_name]
    rows: List[List[Any]] = []
    for m in report.months:
        key = _month_key(m.year, m.month)
        rows.append(base + [key, m.is_mtd, "total", "", round(m.total, 4)])
        for k, v in m.services.items():
            rows.append(base + [key, m.is_mtd, "service", k, round(v, 4)])
        for k, v in m.accounts.items():
            rows.append(base + [key, m.is_mtd, "account",
                                report.account_names.get(k) or k, round(v, 4)])
    for day, amt in (report.daily or []):
        rows.append(base + [day, True, "daily", "", round(amt, 4)])
    return rows


def report_to_dict(report: Report) -> dict:
    return {
        "profile": report.profile,
        "account_id": report.account_id,
        "account_name": report.account_name,
        "cost_metric": report.metric,
        "charge_types_included": list(report.charge_types) or "all",
        "total": round(report.total, 4),
        "months": [{
            "label": m.label,
            "is_mtd": m.is_mtd,
            "total": round(m.total, 4),
            "services": m.services,
            "accounts": {k: {"name": report.account_names.get(k, ""), "cost": v}
                         for k, v in m.accounts.items()},
        } for m in report.months],
        "daily": [{"date": d, "cost": a} for d, a in report.daily]
        if report.daily is not None else None,
    }


# ============================================================================
# CLI
# ============================================================================

def _months_arg(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not a whole number")
    if not 0 <= n <= MAX_MONTHS:
        raise argparse.ArgumentTypeError(
            f"must be between 0 and {MAX_MONTHS} (Cost Explorer look-back)")
    return n


def _charge_types_arg(value: str) -> List[str]:
    if value.strip().lower() == "all":
        return []
    items = [v.strip() for v in value.split(",") if v.strip()]
    if not items:
        raise argparse.ArgumentTypeError("provide a comma-separated list or 'all'")
    return items


def _package_version() -> str:
    try:
        from importlib.metadata import version
        return version("aws-cost-report")
    except Exception:
        return "dev"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    prog = Path(sys.argv[0]).name or "aws-cost-report"
    parser = argparse.ArgumentParser(
        prog=prog,
        description="AWS Cost Summary — clean month-by-month report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  aws-cost-report                                  # asks which profile(s)
  aws-cost-report -p prod -n "Kratos Production"
  aws-cost-report -p prod --months 6 --service-detail --daily
  aws-cost-report -p mgmt --by-account             # org: per child account
  aws-cost-report -p a -p b -p c                   # several profiles
  aws-cost-report --config profiles.json           # profiles from file
  aws-cost-report -p prod --format csv > cost.csv
  aws-cost-report -p prod --export cost.json

profiles.json:
  {"profiles": [{"profile": "stage-humyn", "name": "Stage Humyn"},
                {"profile": "prod-humyn",  "name": "Prod Humyn"}]}
        """,
    )
    parser.add_argument("--profile", "-p", action="append", default=[],
                        help="AWS CLI profile (repeat for several)")
    parser.add_argument("--version", action="version",
                        version=f"aws-cost-report {_package_version()}")
    parser.add_argument("--no-prompt", action="store_true",
                        help="Never ask for a profile; use the default credential chain")
    parser.add_argument("--config", "-c", metavar="FILE",
                        help="JSON file listing profiles and display names")
    parser.add_argument("--account-name", "-n", default="",
                        help="Display name in the header (single profile only)")
    parser.add_argument("--months", "-m", type=_months_arg, default=3,
                        help=f"Past complete months to include, 0-{MAX_MONTHS} (default: 3)")
    parser.add_argument("--service-detail", action="store_true",
                        help="Per-service breakdown for each month")
    parser.add_argument("--by-account", action="store_true",
                        help="Per-linked-account breakdown (run on a management account)")
    parser.add_argument("--daily", "-d", action="store_true",
                        help="Daily spend trend for the current month")
    parser.add_argument("--format", "-f", default="text",
                        choices=["text", "markdown", "csv", "json"],
                        help="Output format (default: text)")
    parser.add_argument("--export", "-e", metavar="FILE",
                        help="Also write the full report to a JSON file")
    parser.add_argument("--metric", choices=METRICS, default=DEFAULT_METRIC,
                        help=f"Cost metric (default: {DEFAULT_METRIC})")
    parser.add_argument("--charge-types", type=_charge_types_arg,
                        default=list(DEFAULT_CHARGE_TYPES), metavar="LIST",
                        help="Comma-separated RECORD_TYPE values, or 'all' for the "
                             "full bill incl. tax/credits (default: Usage,Bundled Discount)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Do not read or write the local cache")
    parser.add_argument("--refresh", action="store_true",
                        help="Ignore cached months and re-fetch (updates the cache)")
    parser.add_argument("--cache-ttl", type=float, default=DEFAULT_CACHE_TTL_HOURS,
                        metavar="HOURS",
                        help=f"Cache lifetime for completed months (default: {DEFAULT_CACHE_TTL_HOURS})")
    parser.add_argument("--workers", type=int, default=4,
                        help="Parallel profiles (default: 4)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Debug logging (shows each Cost Explorer call)")
    return parser.parse_args(argv)


def prompt_for_profiles() -> List[str]:
    """
    Ask the user which AWS profile(s) to use. Prompts go to stderr so that
    csv/json on stdout stays clean. Accepts: number(s), name(s), or 'all'.
    """
    available = sorted(boto3.Session().available_profiles, key=str.lower)
    if not available:
        raise CostReportError(
            "No AWS profiles found in ~/.aws/config or ~/.aws/credentials. "
            "Run `aws configure --profile <name>` first.")

    err = sys.stderr
    err.write("\nAvailable AWS profiles:\n")
    for i, name in enumerate(available, 1):
        err.write(f"  {i:>2}. {name}\n")
    err.write("\nEnter profile number(s) or name(s), comma-separated, "
              "or 'all' (q to quit).\n")

    while True:
        err.write("Profile to use: ")
        err.flush()
        try:
            answer = input().strip()
        except EOFError:
            raise CostReportError("No profile selected.")
        if answer.lower() in ("q", "quit", "exit"):
            raise CostReportError("Cancelled.")
        if answer.lower() == "all":
            return available

        chosen, bad = [], []
        for token in (t.strip() for t in answer.split(",") if t.strip()):
            if token.isdigit() and 1 <= int(token) <= len(available):
                chosen.append(available[int(token) - 1])
            elif token in available:
                chosen.append(token)
            else:
                bad.append(token)
        if chosen and not bad:
            return list(dict.fromkeys(chosen))   # de-duplicate, keep order
        err.write(f"  Not a valid profile: {', '.join(bad) or '(empty)'}. Try again.\n")


def load_targets(opts: argparse.Namespace) -> List[Tuple[Optional[str], str]]:
    """Resolve (profile, display_name) pairs from --config and --profile."""
    targets: List[Tuple[Optional[str], str]] = []
    if opts.config:
        try:
            data = json.loads(Path(opts.config).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CostReportError(f"Cannot read config '{opts.config}': {exc}")
        items = data.get("profiles") if isinstance(data, dict) else data
        if not isinstance(items, list) or not items:
            raise CostReportError("Config must contain a non-empty 'profiles' list.")
        for item in items:
            if isinstance(item, str):
                targets.append((item, ""))
            elif isinstance(item, dict) and item.get("profile"):
                targets.append((item["profile"], item.get("name", "")))
            else:
                raise CostReportError(f"Invalid config entry: {item!r}")
    targets += [(p, "") for p in opts.profile]
    if not targets:
        interactive = (not opts.no_prompt and not os.environ.get("AWS_PROFILE")
                       and sys.stdin.isatty())
        if interactive:
            targets = [(p, "") for p in prompt_for_profiles()]
        else:
            targets = [(None, "")]   # default credential chain / AWS_PROFILE
    if opts.account_name:
        if len(targets) > 1:
            raise CostReportError(
                "--account-name works with a single profile; use --config for names.")
        targets = [(targets[0][0], opts.account_name)]
    return targets


def main(argv: Optional[List[str]] = None) -> int:
    opts = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if opts.verbose else logging.WARNING,
                        format="%(levelname)s: %(message)s")
    logging.getLogger("botocore").setLevel(logging.WARNING)  # keep requests out of logs

    try:
        targets = load_targets(opts)
    except CostReportError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    today = utc_today()

    def run(target: Tuple[Optional[str], str]):
        try:
            return generate_report(target[0], target[1], opts, today), None
        except CostReportError as exc:
            return None, str(exc)

    if len(targets) == 1:
        outcomes = [run(targets[0])]
    else:
        with ThreadPoolExecutor(max_workers=max(1, min(opts.workers, len(targets)))) as pool:
            outcomes = list(pool.map(run, targets))

    reports: List[Report] = []
    failed = 0
    text_parts: List[str] = []
    for (profile, _), (report, error) in zip(targets, outcomes):
        if error:
            failed += 1
            print(f"[ERROR] {profile or 'default'}: {error}", file=sys.stderr)
            continue
        reports.append(report)
        if opts.format == "text":
            text_parts.append(render_text(report, opts))
        elif opts.format == "markdown":
            text_parts.append(render_markdown(report, opts))

    if opts.format in ("text", "markdown"):
        print("\n".join(text_parts))
        if opts.format == "text" and len(reports) > 1:
            print(f"  Combined Total Cost ({len(reports)} accounts) : "
                  f"{_fmt_usd(sum(r.total for r in reports))}\n")
    elif opts.format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(CSV_HEADER)
        for r in reports:
            writer.writerows(csv_rows(r))
        sys.stdout.write(buf.getvalue())

    payload = None
    if opts.format == "json" or opts.export:
        payload = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "combined_total": round(sum(r.total for r in reports), 4),
            "reports": [report_to_dict(r) for r in reports],
        }
    if opts.format == "json":
        print(json.dumps(payload, indent=2))
    if opts.export and payload is not None:
        try:
            Path(opts.export).write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"[OK] JSON exported → {opts.export}", file=sys.stderr)
        except OSError as exc:
            print(f"[ERROR] Cannot write {opts.export}: {exc}", file=sys.stderr)
            failed += 1

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
