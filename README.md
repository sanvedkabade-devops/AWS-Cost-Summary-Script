# AWS Cost Summary Report

A CLI tool that fetches AWS account cost data and displays it in a clean,
readable format — month-by-month totals with current month marked as
Till Date.

---

## Sample Output

```
AWS Cost Summary
Kratos Production
Account ID: 183295421905

  • July 2026       : $3,967.16
  • August 2026     : $5,136.25
  • September 2026  : $6,527.29
  • October 2026    : $4,346.38 (Till Date)

  Total Cost : $19,977.07
```

---

## What It Reports

| Section | Description |
|---|---|
| Monthly totals | Cost for each of the last N complete months |
| Till Date | Current month spend up to today |
| Total Cost | Sum of all months shown |
| Per-service detail | Per-service breakdown per month (optional, via `--service-detail`) |
| Daily trend | Day-by-day spend for current month (optional, via `--daily`) |
| JSON export | Machine-readable dump of all data (optional, via `--export`) |

> Costs shown are **Usage only** — Credits, Tax, Refunds, Support Fees, and
> Bundled Discounts are handled exactly as the AWS Cost Explorer console
> displays them.

---

## Requirements

| Requirement | Version |
|---|---|
| Python | 3.8 or higher |
| pip | Any recent version |
| AWS CLI | For `aws configure` |

### Check Python version

```bash
python3 --version
```

If Python 3.8+ is not installed:

```bash
# Ubuntu / Debian
sudo apt update && sudo apt install python3 python3-pip python3-venv -y

# Amazon Linux / RHEL
sudo yum install python3 -y
```

---

## IAM Permissions Required

Attach this policy to the IAM user or role whose credentials you will use:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ce:GetCostAndUsage",
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    }
  ]
}
```

> AWS Cost Explorer must also be enabled in the account.
> Go to: **AWS Console → Billing → Cost Explorer → Enable Cost Explorer**
> It can take up to 24 hours after first activation for data to appear.

---

## Setup

### Step 1 — Clone or copy the project

```bash
cd ~
git clone https://github.com/sanvedkabade-devops/AWS-Cost-Summary-Script.git
cd AWS-Cost-Summary-Script
```

Or if you already have the files:

```bash
cd ~/AWS-Cost-Summary-Script
```

### Step 2 — Configure AWS credentials

```bash
aws configure --profile <profile-name>
```

It will prompt you for:

```
AWS Access Key ID     : AKIAxxxxxxxxxxxxxxxx
AWS Secret Access Key : xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
Default region name   : us-east-1
Default output format : json
```

Example:

```bash
aws configure --profile naman
aws configure --profile production
aws configure --profile staging
```

### Step 3 — Make the launcher executable (one-time only)

```bash
chmod +x run_cost_report.sh
```

### Step 4 — Run

```bash
./run_cost_report.sh --profile naman --account-name "Kratos Production"
```

The launcher automatically:
- Creates the Python virtual environment
- Installs all dependencies
- Runs the script

**No need to manually activate a virtualenv or install packages.**

---

## Manual Setup (if you prefer not to use the launcher)

### Step 1 — Create a virtual environment

```bash
python3 -m venv .venv
```

### Step 2 — Activate the virtual environment

```bash
source .venv/bin/activate
```

Your terminal prompt will show `(.venv)` confirming it is active.

### Step 3 — Install dependencies

```bash
pip install -r requirements.txt
```

### Step 4 — Configure AWS credentials

**Option A — AWS CLI (recommended, stores permanently)**

```bash
aws configure --profile <profile-name>
```

It will prompt you for:

```
AWS Access Key ID     : AKIAxxxxxxxxxxxxxxxx
AWS Secret Access Key : xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
Default region name   : us-east-1
Default output format : json
```

**Option B — Environment variables (current session only)**

```bash
export AWS_ACCESS_KEY_ID=AKIAxxxxxxxxxxxxxxxx
export AWS_SECRET_ACCESS_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
export AWS_DEFAULT_REGION=us-east-1
```

### Step 5 — Verify credentials are working

```bash
aws sts get-caller-identity --profile <profile-name>
```

Expected output:

```json
{
    "UserId": "AIDAXXXXXXXXXXXXXXXXX",
    "Account": "183295421905",
    "Arn": "arn:aws:iam::183295421905:user/naman"
}
```

### Step 6 — Run

```bash
python3 aws_cost_report.py --profile naman --account-name "Kratos Production"
```

---

## Usage

```bash
python aws_cost_report.py [OPTIONS]
```

| Flag | Short | Default | Description |
|---|---|---|---|
| `--profile` | `-p` | default | AWS CLI profile name |
| `--account-name` | `-n` | — | Friendly name shown in the report header |
| `--months` | `-m` | `3` | Number of past complete months to show |
| `--service-detail` | — | off | Show per-service breakdown for each month |
| `--daily` | `-d` | off | Show daily spend trend for current month |
| `--export FILE` | `-e` | — | Export all data to a JSON file |
| `--verbose` | `-v` | off | Enable debug logging |

---

## Commands

### Basic report — last 3 months + current MTD

```bash
python aws_cost_report.py --profile naman
```

### With a friendly account name in the header

```bash
python aws_cost_report.py --profile naman --account-name "Kratos Production"
```

### Change the number of months

```bash
# Last 6 months
python aws_cost_report.py --profile naman --months 6

# Last 12 months
python aws_cost_report.py --profile naman --months 12
```

### Per-service breakdown for each month

```bash
python aws_cost_report.py --profile naman --service-detail
```

### Daily spend trend for current month

```bash
python aws_cost_report.py --profile naman --daily
```

### Export report to JSON

```bash
python aws_cost_report.py --profile naman --export report_oct2026.json
```

### Save output to a text file

```bash
python aws_cost_report.py --profile naman --account-name "Kratos Production" > report.txt
```

### Full report — all options combined

```bash
python aws_cost_report.py \
  --profile naman \
  --account-name "Kratos Production" \
  --months 6 \
  --service-detail \
  --daily \
  --export report_oct2026.json
```

### Run without activating the virtualenv (using launcher)

```bash
chmod +x run_cost_report.sh
./run_cost_report.sh --profile naman --account-name "Kratos Production"
```

The launcher handles the virtualenv activation automatically.

---

## Running for Multiple Accounts

If you manage multiple AWS accounts, configure a named profile for each and
run the script once per account:

```bash
aws configure --profile kratos-prod
aws configure --profile kratos-stage
aws configure --profile spline

python aws_cost_report.py --profile kratos-prod  --account-name "Kratos Production"
python aws_cost_report.py --profile kratos-stage --account-name "Stage Kratos"
python aws_cost_report.py --profile spline        --account-name "Spline AI Production"
```

---

## Troubleshooting

**`Command 'python' not found`**

Use `python3` instead:
```bash
python3 aws_cost_report.py --profile naman
```

**`boto3 is not installed`**

The virtual environment is not active. Run:
```bash
source .venv/bin/activate
pip install -r requirements.txt
```

**`No AWS credentials found`**

Run `aws configure --profile <name>` or export environment variables.

**`Access Denied: ce:GetCostAndUsage`**

The IAM user does not have Cost Explorer permission. Attach the IAM policy
from the Requirements section above.

**`Cost Explorer not enabled`**

Enable it at: AWS Console → Billing → Cost Explorer → Enable.
Data appears within 24 hours of first activation.

**Numbers differ slightly from the console**

Ensure you are comparing against the **Unblended Cost** metric with
**Charge type: Usage** filter in the console. The script matches this
view exactly.

---

## File Structure

```
AWS-Cost-Summary-Script/
├── aws_cost_report.py   # Main Python script
├── run_cost_report.sh   # Shell launcher (handles venv automatically)
├── requirements.txt     # Pinned Python dependencies
└── README.md            # This file
```

---

## Notes

- AWS Cost Explorer charges **$0.01 per API request**. A standard run
  makes 4–8 API calls depending on flags used.
- Cost data has a **24–48 hour lag** — yesterday's spend may not appear
  until tomorrow.
- Costs are **Unblended** by default. To switch to Blended, change
  `COST_METRIC = "UnblendedCost"` to `"BlendedCost"` in `aws_cost_report.py`.
