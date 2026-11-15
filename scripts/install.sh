#!/usr/bin/env bash
# StockWatch installer (macOS). Idempotent: safe to re-run after `git pull`.
#  1. creates .venv (uv if available, else python3 -m venv) and installs the package
#  2. checks Google Chrome is installed (we drive real Chrome via channel=chrome,
#     so no Playwright browser download is needed)
#  3. creates ~/StockWatch (config.yaml, .env template chmod 600, chrome-profile chmod 700)
#  4. runs `stockwatch install` to load the launchd agent
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO/.venv"
SW_HOME="${STOCKWATCH_HOME:-$HOME/StockWatch}"
CHROME_APP="/Applications/Google Chrome.app"

say()  { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || warn "StockWatch targets macOS; launchd steps will fail elsewhere."

# ---------------------------------------------------------------- venv + package
if command -v uv >/dev/null 2>&1; then
  say "Creating virtualenv with uv at $VENV"
  [[ -x "$VENV/bin/python" ]] || uv venv --python 3.12 "$VENV"
  uv pip install -p "$VENV/bin/python" -e "$REPO"
else
  PY="$(command -v python3.12 || command -v python3 || true)"
  [[ -n "$PY" ]] || die "python3 (3.12+) not found; install it or install uv (https://docs.astral.sh/uv/)"
  "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
    || die "$PY is older than 3.12; install Python 3.12+ or uv"
  say "Creating virtualenv with $PY -m venv at $VENV"
  [[ -x "$VENV/bin/python" ]] || "$PY" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --upgrade pip >/dev/null
  "$VENV/bin/python" -m pip install -e "$REPO"
fi
SW="$VENV/bin/stockwatch"

# ---------------------------------------------------------------- Chrome
if [[ -d "$CHROME_APP" || -d "$HOME$CHROME_APP" ]]; then
  say "Google Chrome found"
else
  warn "Google Chrome not found in /Applications. Install it from https://www.google.com/chrome/ ;"
  warn "StockWatch uses the real Chrome (channel=chrome) and does not download a bundled browser."
fi

# ---------------------------------------------------------------- ~/StockWatch
say "Preparing $SW_HOME"
mkdir -p "$SW_HOME/logs" "$SW_HOME/chrome-profile"
chmod 700 "$SW_HOME/chrome-profile"

if [[ ! -f "$SW_HOME/config.yaml" ]]; then
  if [[ -f "$REPO/config.example.yaml" ]]; then
    cp "$REPO/config.example.yaml" "$SW_HOME/config.yaml"
    say "Created $SW_HOME/config.yaml from config.example.yaml (edit home.zip and watches)"
  else
    STOCKWATCH_HOME="$SW_HOME" "$SW" init
  fi
else
  say "Keeping existing $SW_HOME/config.yaml"
fi

if [[ ! -f "$SW_HOME/.env" ]]; then
  ( umask 077; cat > "$SW_HOME/.env" <<'ENV'
# StockWatch secrets. Referenced from config.yaml as ${NAME}. Never commit this file.
# Alternatively store them in the macOS Keychain:
#   security add-generic-password -s stockwatch -a PUSHOVER_USER_KEY -w '<value>'
PUSHOVER_USER_KEY=
PUSHOVER_APP_TOKEN=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DASHBOARD_PIN=
HEARTBEAT_URL=
BESTBUY_API_KEY=
ENV
  )
  say "Created $SW_HOME/.env template; fill in your secrets"
fi
chmod 600 "$SW_HOME/.env"

# ---------------------------------------------------------------- launchd
say "Installing launchd agent"
STOCKWATCH_HOME="$SW_HOME" "$SW" install

cat <<EOF

Done. Next steps:
  1. Edit $SW_HOME/config.yaml and $SW_HOME/.env
  2. $SW setup-profile   # sign in to each retailer in the dedicated Chrome profile
  3. $SW doctor          # verify config, secrets, alerts and adapters
  4. $SW test-alert
  System Settings > Battery/Energy: prevent automatic sleep when on power.
Logs: $SW_HOME/logs/stockwatch.log
EOF
