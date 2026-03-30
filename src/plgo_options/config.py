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
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")