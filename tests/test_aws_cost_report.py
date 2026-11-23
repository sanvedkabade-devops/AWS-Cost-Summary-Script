"""Offline unit tests (no AWS calls). Run: python -m unittest discover -s tests"""
import argparse
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import aws_cost_report as acr  # noqa: E402


class FakeCE:
    """Minimal Cost Explorer stand-in that records calls."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get_cost_and_usage(self, **params):
        self.calls.append(params)
        return self.pages.pop(0)


def grouped(month_start, **services):
    return {"TimePeriod": {"Start": month_start},
            "Groups": [{"Keys": [k], "Metrics": {"UnblendedCost": {"Amount": str(v)}}}
                       for k, v in services.items()]}


def total(month_start, amount):
    return {"TimePeriod": {"Start": month_start},
            "Total": {"UnblendedCost": {"Amount": str(amount)}}}


class DateTests(unittest.TestCase):
    def test_subtract_months_wraps_year(self):
        self.assertEqual(acr._subtract_months(date(2026, 2, 15), 3), date(2025, 11, 1))
        self.assertEqual(acr._subtract_months(date(2026, 1, 1), 12), date(2025, 1, 1))

    def test_months_to_show(self):
        got = acr.months_to_show(date(2026, 11, 5), 3)
        self.assertEqual(got, [(2026, 8, False), (2026, 9, False),
                               (2026, 10, False), (2026, 11, True)])

    def test_zero_months_is_current_only(self):
        self.assertEqual(acr.months_to_show(date(2026, 1, 1), 0), [(2026, 1, True)])

    def test_fmt_usd(self):
        self.assertEqual(acr._fmt_usd(9867.656), "$9,867.66")
        self.assertEqual(acr._fmt_usd(-5), "-$5.00")
        self.assertEqual(acr._fmt_usd(-0.001), "$0.00")


class ArgTests(unittest.TestCase):
    def test_months_validation(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            acr._months_arg("-1")
        with self.assertRaises(argparse.ArgumentTypeError):
            acr._months_arg("13")
        self.assertEqual(acr._months_arg("6"), 6)

    def test_charge_types(self):
        self.assertEqual(acr._charge_types_arg("Usage, Tax"), ["Usage", "Tax"])
        self.assertEqual(acr._charge_types_arg("ALL"), [])

    def test_filter(self):
        self.assertIsNone(acr.CostQuery(charge_types=()).ce_filter())
        f = acr.CostQuery().ce_filter()
        self.assertEqual(f["Dimensions"]["Values"], ["Usage", "Bundled Discount"])


class FetchTests(unittest.TestCase):
    today = date(2026, 11, 5)
    months = acr.months_to_show(today, 2)   # Sep, Oct, Nov(MTD)

    def cache(self, d, **kw):
        return acr.CostCache("111", 24, directory=Path(d), **kw)

    def test_single_ranged_call_and_totals(self):
        ce = FakeCE([{"ResultsByTime": [total("2026-09-01", 10), total("2026-10-01", 20),
                                        total("2026-11-01", 5)]}])
        with tempfile.TemporaryDirectory() as d:
            res = acr.fetch_monthly(ce, self.months, self.today, acr.CostQuery(),
                                    None, self.cache(d))
        self.assertEqual(len(ce.calls), 1)
        self.assertEqual(ce.calls[0]["TimePeriod"], {"Start": "2026-09-01", "End": "2026-11-06"})
        self.assertEqual(sum(res[(2026, 10)].values()), 20)

    def test_cache_skips_completed_months(self):
        pages = [{"ResultsByTime": [total("2026-09-01", 10), total("2026-10-01", 20),
                                    total("2026-11-01", 5)]},
                 {"ResultsByTime": [total("2026-11-01", 7)]}]
        ce = FakeCE(pages)
        with tempfile.TemporaryDirectory() as d:
            acr.fetch_monthly(ce, self.months, self.today, acr.CostQuery(), None, self.cache(d))
            res = acr.fetch_monthly(ce, self.months, self.today, acr.CostQuery(), None, self.cache(d))
            self.assertEqual(ce.calls[1]["TimePeriod"]["Start"], "2026-11-01")
            self.assertEqual(sum(res[(2026, 9)].values()), 10)    # from cache
            self.assertEqual(sum(res[(2026, 11)].values()), 7)    # live
            # --refresh ignores the cache
            ce2 = FakeCE([{"ResultsByTime": []}])
            acr.fetch_monthly(ce2, self.months, self.today, acr.CostQuery(), None,
                              self.cache(d, refresh=True))
            self.assertEqual(ce2.calls[0]["TimePeriod"]["Start"], "2026-09-01")

    def test_cache_slot_depends_on_metric_and_filter(self):
        ce = FakeCE([{"ResultsByTime": [total("2026-09-01", 10)]},
                     {"ResultsByTime": [total("2026-09-01", 99)]}])
        with tempfile.TemporaryDirectory() as d:
            acr.fetch_monthly(ce, self.months, self.today, acr.CostQuery(), None, self.cache(d))
            acr.fetch_monthly(ce, self.months, self.today, acr.CostQuery(charge_types=()),
                              None, self.cache(d))
        self.assertEqual(len(ce.calls), 2)

    def test_grouped_and_paginated(self):
        ce = FakeCE([
            {"ResultsByTime": [grouped("2026-11-01", EC2=3)], "NextPageToken": "t"},
            {"ResultsByTime": [grouped("2026-11-01", EC2=2, S3=1)]},
        ])
        with tempfile.TemporaryDirectory() as d:
            res = acr.fetch_monthly(ce, [(2026, 11, True)], self.today, acr.CostQuery(),
                                    acr.DIM_SERVICE, self.cache(d))
        self.assertEqual(res[(2026, 11)], {"EC2": 5.0, "S3": 1.0})
        self.assertEqual(ce.calls[1]["NextPageToken"], "t")
        self.assertEqual(ce.calls[0]["GroupBy"], [{"Type": "DIMENSION", "Key": "SERVICE"}])


class RenderTests(unittest.TestCase):
    def test_text_layout_matches_template(self):
        months = [acr.MonthResult(2026, 9, False, 6587.85),
                  acr.MonthResult(2026, 10, True, 2768.65)]
        rep = acr.Report("p", "123456789012", "Kratos Production", "UnblendedCost",
                         acr.DEFAULT_CHARGE_TYPES, months)
        opts = argparse.Namespace(service_detail=False, by_account=False)
        self.assertEqual(acr.render_text(rep, opts).splitlines(), [
            "", "AWS Cost Summary", "Kratos Production", "Account ID: 123456789012", "",
            "  • September 2026  : $6,587.85",
            "  • October 2026    : $2,768.65 (Till Date)",
            "", "  Total Cost : $9,356.50"])

    def test_csv_rows(self):
        m = acr.MonthResult(2026, 10, True, 3.0, services={"EC2": 3.0})
        rep = acr.Report("p", "1", "N", "UnblendedCost", (), [m])
        rows = acr.csv_rows(rep)
        self.assertEqual([r[5] for r in rows], ["total", "service"])


class PromptTests(unittest.TestCase):
    def run_prompt(self, answers):
        sess = mock.Mock(available_profiles=["stage", "Prod", "dev"])
        with mock.patch.object(acr.boto3, "Session", return_value=sess), \
             mock.patch("builtins.input", side_effect=answers), \
             mock.patch.object(sys, "stderr", mock.Mock()):
            return acr.prompt_for_profiles()

    def test_by_number_and_name_dedup(self):
        # sorted case-insensitively: dev, Prod, stage
        self.assertEqual(self.run_prompt(["1, stage, 3"]), ["dev", "stage"])

    def test_all(self):
        self.assertEqual(self.run_prompt(["all"]), ["dev", "Prod", "stage"])

    def test_retry_on_invalid(self):
        self.assertEqual(self.run_prompt(["nope", "2"]), ["Prod"])

    def test_quit(self):
        with self.assertRaises(acr.CostReportError):
            self.run_prompt(["q"])

    def test_not_prompted_when_profile_given_or_no_tty(self):
        base = dict(config=None, account_name="", no_prompt=False)
        with mock.patch.object(acr, "prompt_for_profiles") as prompt:
            acr.load_targets(argparse.Namespace(profile=["x"], **base))
            with mock.patch.object(sys.stdin, "isatty", return_value=False):
                self.assertEqual(acr.load_targets(argparse.Namespace(profile=[], **base)),
                                 [(None, "")])
            prompt.assert_not_called()

    def test_prompted_on_tty(self):
        base = dict(config=None, account_name="", no_prompt=False)
        with mock.patch.dict(acr.os.environ, {}, clear=True), \
             mock.patch.object(sys.stdin, "isatty", return_value=True), \
             mock.patch.object(acr, "prompt_for_profiles", return_value=["a"]):
            self.assertEqual(acr.load_targets(argparse.Namespace(profile=[], **base)),
                             [("a", "")])


if __name__ == "__main__":
    unittest.main()
