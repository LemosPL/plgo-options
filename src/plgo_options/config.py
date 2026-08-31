"""Application configuration."""

from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=True)
except ImportError:
    pass  # dotenv not installed — use environment variables directly

DERIBIT_BASE_URL = "https://www.deribit.com/api/v2"
DEFAULT_CURRENCY = "ETH"
REQUEST_TIMEOUT = 10

# Deribit private API credentials (for order execution)
# Set DERIBIT_TESTNET=0 to use production (default: testnet for safety)
DERIBIT_CLIENT_ID = os.environ.get("DERIBIT_CLIENT_ID", "")
DERIBIT_CLIENT_SECRET = os.environ.get("DERIBIT_CLIENT_SECRET", "")
DERIBIT_TESTNET = os.environ.get("DERIBIT_TESTNET", "1") == "1"
DERIBIT_EXEC_URL = "https://test.deribit.com/api/v2" if DERIBIT_TESTNET else DERIBIT_BASE_URL

# Anthropic API
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
# The Action Radar brief gets its own model knob so tuning it can't disturb the
# optimizer chat (which shares long cached prefixes tuned for its own model).
ANTHROPIC_BRIEF_MODEL = os.environ.get("ANTHROPIC_BRIEF_MODEL", "claude-opus-5")

# ── Action Radar briefing / alerting ──────────────────────────────────────
# Slack incoming-webhook URL. In prod this comes from Secret Manager; locally
# from .env. Empty = brief generation still works, it just isn't delivered.
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# Shared secret for the scheduler-driven endpoints (daily brief, proximity
# poller, IV snapshot). Cloud Scheduler sends it as X-Signals-Token. Empty
# disables the check — fine locally, set it in prod so a public Cloud Run URL
# can't be triggered by anyone who finds it.
SIGNALS_TOKEN = os.environ.get("SIGNALS_TOKEN", "")

# Timezone the brief is stamped in (the 09:00 cadence is London-based).
BRIEF_TIMEZONE = os.environ.get("BRIEF_TIMEZONE", "Europe/London")

# Quiet hours (local BRIEF_TIMEZONE, 24h). Inside this window, "approaching"
# (10%) pings are held back and folded into the next morning brief; "imminent"
# (5%) and level crossings always go out. Set QUIET_HOURS=off for 24/7.
QUIET_HOURS = os.environ.get("QUIET_HOURS", "22:00-07:00")

# Minimum hours before the same (signal, band) can alert again.
ALERT_COOLDOWN_HOURS = float(os.environ.get("ALERT_COOLDOWN_HOURS", "6"))