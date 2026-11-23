#!/usr/bin/env bash
# =============================================================================
# run_cost_report.sh — AWS Cost Report Launcher
# =============================================================================
# Usage:
#   ./run_cost_report.sh [options]
#
# This script:
#   1. Checks for Python 3 and pip
#   2. Creates a virtualenv (if not present)
#   3. Installs dependencies from requirements.txt (only when it changes)
#   4. Detects or prompts for AWS credentials
#   5. Runs aws_cost_report.py with any arguments passed to this script
#      (with no --profile it asks which AWS profile to use)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
REQUIREMENTS="${SCRIPT_DIR}/requirements.txt"
PYTHON_SCRIPT="${SCRIPT_DIR}/aws_cost_report.py"

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()    { echo -e "${CYAN}[INFO]${NC}  $*" >&2; }
success() { echo -e "${GREEN}[OK]${NC}    $*" >&2; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*" >&2; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ---------------------------------------------------------------------------
# 1. Verify Python 3 is available
# ---------------------------------------------------------------------------
if command -v python3 &>/dev/null; then
    PYTHON_BIN="python3"
elif command -v python &>/dev/null && python --version 2>&1 | grep -q "Python 3"; then
    PYTHON_BIN="python"
else
    error "Python 3 is required but not found."
    error "Install it via: sudo apt install python3  (Debian/Ubuntu)"
    error "                  sudo yum install python3  (RHEL/Amazon Linux)"
    exit 1
fi

PYTHON_VERSION=$("$PYTHON_BIN" --version 2>&1 | awk '{print $2}')
info "Python version: ${PYTHON_VERSION}"

# ---------------------------------------------------------------------------
# 2. Create virtualenv if it doesn't exist
# ---------------------------------------------------------------------------
if [ ! -d "${VENV_DIR}" ]; then
    info "Creating virtual environment at ${VENV_DIR} ..."
    "$PYTHON_BIN" -m venv "${VENV_DIR}"
    success "Virtual environment created."
fi

VENV_PYTHON="${VENV_DIR}/bin/python"
VENV_PIP="${VENV_DIR}/bin/pip"

# ---------------------------------------------------------------------------
# 3. Install dependencies (only when requirements.txt changed)
# ---------------------------------------------------------------------------
STAMP_FILE="${VENV_DIR}/.requirements.stamp"
if [ -f "${REQUIREMENTS}" ]; then
    REQ_STAMP="$(cksum < "${REQUIREMENTS}")"
    if [ ! -f "${STAMP_FILE}" ] || [ "$(cat "${STAMP_FILE}")" != "${REQ_STAMP}" ]; then
        info "Installing dependencies from requirements.txt ..."
        "$VENV_PIP" install --quiet -r "${REQUIREMENTS}"
        echo "${REQ_STAMP}" > "${STAMP_FILE}"
        success "Dependencies installed."
    fi
else
    warn "requirements.txt not found — installing boto3 directly."
    "$VENV_PIP" install --quiet boto3
fi

# ---------------------------------------------------------------------------
# 4. Credential check
# ---------------------------------------------------------------------------
check_credentials() {
    # Priority order:
    #   a) Explicit env vars
    #   b) ~/.aws/credentials (default or named profile)
    #   c) EC2/ECS/Lambda instance metadata (auto, no action needed)

    if [ -n "${AWS_ACCESS_KEY_ID:-}" ] && [ -n "${AWS_SECRET_ACCESS_KEY:-}" ]; then
        success "AWS credentials detected via environment variables."
        return 0
    fi

    if [ -f "${HOME}/.aws/credentials" ] || [ -f "${HOME}/.aws/config" ]; then
        success "AWS credentials file found at ~/.aws."
        return 0
    fi

    # Try IMDSv2 (EC2 instance metadata)
    if curl -s --max-time 1 -o /dev/null \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 10" \
        -X PUT "http://169.254.169.254/latest/api/token" 2>/dev/null; then
        success "Running on EC2 — will use instance profile credentials."
        return 0
    fi

    warn "No AWS credentials detected."
    echo ""
    echo "  Choose one of the following options:"
    echo ""
    echo "  [1] Run 'aws configure' to save credentials locally"
    echo "  [2] Export environment variables:"
    echo "        export AWS_ACCESS_KEY_ID=<your-key>"
    echo "        export AWS_SECRET_ACCESS_KEY=<your-secret>"
    echo "        export AWS_SESSION_TOKEN=<token>   # only for temporary credentials"
    echo "  [3] Attach an IAM Role to this instance"
    echo ""
    read -r -p "  Do you want to run 'aws configure' now? [y/N]: " response
    if [[ "$(printf %s "${response}" | tr '[:upper:]' '[:lower:]')" == "y" ]]; then
        aws configure
    else
        warn "Proceeding without confirmed credentials — the script may fail."
    fi
}

check_credentials

# ---------------------------------------------------------------------------
# 5. Run the cost report
# ---------------------------------------------------------------------------
# Banner goes to stderr so `--format csv|json` output stays clean.
{
    echo ""
    echo "================================================================"
    echo "  Fetching AWS Cost Data ..."
    echo "================================================================"
} >&2

"$VENV_PYTHON" "${PYTHON_SCRIPT}" "$@"
