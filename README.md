# AWS Cost Summary Report

A CLI tool that fetches AWS cost data and prints a clean month-by-month
summary, with the current month marked as Till Date. It works across several
AWS profiles (organisations or accounts) in one run.

---

## Sample Output

```
AWS Cost Summary
Kratos Production
Account ID: 123456789012

  • July 2026       : $3,967.16
  • August 2026     : $5,136.25
  • September 2026  : $6,527.29
  • October 2026    : $4,346.38 (Till Date)

  Total Cost : $19,977.07
```

---

## What It Reports

| Section | Flag | Description |
|---|---|---|
| Monthly totals | (default) | Last N complete months plus the current month |
| Till Date | (default) | Current month spend up to today (UTC) |
| Per-service detail | `--service-detail` | Per-service breakdown per month |
| Per-account detail | `--by-account` | Per linked (child) account breakdown, with account names |
| Daily trend | `--daily` | Day-by-day spend for the current month |
| Output formats | `--format` | `text` (default), `markdown`, `csv`, `json` |
| JSON file | `--export FILE` | Writes the full report to a file as well |

By default the numbers are **Unblended cost, charge type Usage (plus Bundled
Discount)**, which matches the Cost Explorer console with the filter
*Charge type: Usage*. Credits, Tax, Refunds, Support fees and RI/Savings Plan
fees are excluded, so totals will not equal your invoice. To get the full bill
use `--charge-types all`. To change the metric use `--metric`
(`BlendedCost`, `AmortizedCost`, `NetUnblendedCost`, `NetAmortizedCost`).

---

## Requirements

| Requirement | Version |
|---|---|
| Python | 3.8 or higher |
| AWS CLI | For `aws configure` / `aws sso login` |

IAM permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ce:GetCostAndUsage",
        "ce:GetDimensionValues",
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    }
  ]
}
```

`ce:GetDimensionValues` is only needed for `--by-account` (account names).
Cost Explorer must be enabled in the account (Console → Billing → Cost
Explorer). Data can take up to 24 hours to appear after activation.

---

## Install as a command

```bash
# Recommended: isolated install, command available everywhere
uv tool install .            # or: pipx install .
# Developing on the script? Use an editable install instead:
uv tool install --editable .

aws-cost-report              # asks which profile to use
aws-cost-report --version
```

Without uv or pipx: `python3 -m venv .venv && .venv/bin/pip install .`, then
run `.venv/bin/aws-cost-report`. To remove it: `uv tool uninstall aws-cost-report`.

All examples below use `aws-cost-report`. If you prefer not to install it,
`./run_cost_report.sh [options]` or `python aws_cost_report.py [options]`
accept the same options.

## Choosing a profile

With no `--profile` or `--config`, the script **asks which AWS profile to
use**. It lists the profiles from `~/.aws/config` and `~/.aws/credentials`.
Answer with a number, a name, several comma-separated, or `all`:

```
Available AWS profiles:
   1. prod-humyn
   2. stage-humyn
   ...
Profile to use: 2
```

The prompt is skipped (the default credential chain is used) when you pass
`--profile` or `--config`, when `AWS_PROFILE` is set, with `--no-prompt`, or
when the script is not attached to a terminal (cron, pipes).

`./run_cost_report.sh` creates `.venv`, installs `requirements.txt` only when
it changes, and runs the script. Use it if you do not want to install the command.

### AWS credentials

Use any normal method: `aws configure --profile <name>`, `aws sso login
--profile <name>`, environment variables, or an IAM role. Verify with:

```bash
aws sts get-caller-identity --profile <name>
```

---

## Usage

```bash
aws-cost-report [OPTIONS]
```

| Flag | Short | Default | Description |
|---|---|---|---|
| `--profile NAME` | `-p` | asks | AWS profile. Repeat for several |
| `--config FILE` | `-c` | — | JSON file with profiles and display names |
| `--account-name` | `-n` | — | Header name (single profile only) |
| `--months N` | `-m` | `3` | Past complete months, 0–12 |
| `--service-detail` | — | off | Per-service breakdown |
| `--by-account` | — | off | Per linked-account breakdown |
| `--daily` | `-d` | off | Daily trend for the current month |
| `--format` | `-f` | `text` | `text`, `markdown`, `csv`, `json` |
| `--export FILE` | `-e` | — | Also write the full report as JSON |
| `--metric` | — | `UnblendedCost` | Cost metric |
| `--charge-types LIST` | — | `Usage,Bundled Discount` | RECORD_TYPE values, or `all` |
| `--no-prompt` | — | off | Never ask for a profile |
| `--no-cache` / `--refresh` | — | off | Skip the cache / re-fetch and update it |
| `--cache-ttl HOURS` | — | `24` | Cache lifetime for completed months |
| `--workers N` | — | `4` | Profiles queried in parallel |
| `--verbose` | `-v` | off | Show each Cost Explorer call |

### Examples

```bash
# One profile, with a header name
aws-cost-report -p prod-humyn -n "Prod Humyn"

# 6 months with per-service detail and daily trend
aws-cost-report -p prod-humyn --months 6 --service-detail --daily

# Management account of an organisation: costs per child account
aws-cost-report -p kratos-management --by-account

# Several profiles in one run (shows a combined total at the end)
aws-cost-report -p stage-humyn -p prod-humyn

# CSV or Markdown for sharing
aws-cost-report -p prod-humyn --format csv > cost.csv
aws-cost-report -p prod-humyn --format markdown > cost.md
```

### Many organisations: `--config`

Copy `profiles.example.json` to `profiles.json` (git-ignored) and list your
profiles:

```json
{
  "profiles": [
    { "profile": "stage-humyn", "name": "Stage Humyn" },
    { "profile": "prod-humyn",  "name": "Prod Humyn" }
  ]
}
```

```bash
aws-cost-report --config profiles.json --by-account
```

Profiles run in parallel and print in config order. A profile that fails
(missing, expired SSO login, no permission) prints one `[ERROR]` line to
stderr and the others still run. The exit code is 1 if any profile failed.

### Management vs member accounts

Cost Explorer scope depends on where you run it. A **management account**
returns the consolidated cost of every child account, and `--by-account`
splits it per account. A **member account** only sees its own cost.

---

## How It Stays Cheap

Cost Explorer charges **$0.01 per API request**.

- One ranged monthly query returns every month at once, instead of one call
  per month. A default run makes **1 call**. `--service-detail` and
  `--by-account` add one call each, and `--by-account` adds a second call for
  account names. `--daily` adds one more.
- Completed months are cached in `~/.cache/aws-cost-report/<account-id>.json`
  for 24 hours, so a repeat run only fetches the current month. The cache key
  includes the metric and charge types. Set `AWS_COST_REPORT_CACHE` to move
  the cache directory.
- Cost data lags 24–48 hours. Use `--refresh` after late adjustments such as
  credits or refunds.

---

## Tests

```bash
python -m unittest discover -s tests -t .
```

The tests run offline against a fake Cost Explorer client.

---

## Troubleshooting

| Message | Fix |
|---|---|
| `AWS profile 'x' not found` | `aws configure --profile x` |
| `AWS SSO session expired` | `aws sso login --profile x` |
| `No AWS credentials found` | Run `aws configure` or export credentials |
| `Access denied` | Attach the IAM policy above |
| `Cost Explorer: ...ValidationException` | Look-back is limited to 12 months unless multi-year data is enabled |
| Numbers differ from the invoice | Defaults exclude tax, credits and refunds. Try `--charge-types all` |
| `python: command not found` | Use `python3` |

---

## File Structure

```
AWS-Cost-Summary-Script/
├── aws_cost_report.py       # Main script (entry point: aws-cost-report)
├── pyproject.toml           # Packaging / console command
├── run_cost_report.sh       # Launcher (venv + dependencies)
├── requirements.txt         # Pinned dependencies
├── profiles.example.json    # Example --config file
├── tests/                   # Offline unit tests
└── README.md
```
