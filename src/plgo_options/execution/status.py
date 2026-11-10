"""Where the Deribit ETH connector stands on each network, for the Connectors screen.

Connection is checked live on each call, read-only. Test results can't be
measured from a running server, so ``TESTS`` is a record: change a connector to
"passed" only after its suite has really been run against that network.
A connector is only ever called ready when both agree.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from plgo_options.config import DERIBIT_CONNECTOR_PRODUCTION, DERIBIT_TESTNET

from .deribit import ConnectorError, DeribitConnector, deribit_keys

TESTS: dict[str, dict[str, str]] = {
    "deribit-testnet": {"status": "not_run", "detail": "tests/test_deribit_testnet.py, 15 scenarios"},
    "deribit-production": {"status": "not_run", "detail": "Runs after testnet"},
}


def _connection(probe: dict[str, Any]) -> tuple[str, bool]:
    if not probe["configured"]:
        return "No key", False
    if probe["unreachable"]:
        return "Unreachable", False
    if not probe["authenticated"]:
        return "Key refused", False
    if probe["healthy"]:
        return "Connected", True
    return ("Connected, ETH locked" if probe["locked"] else "Connected, unhealthy"), False


def _key_step(probe: dict[str, Any], network: str, variables: str) -> str | None:
    """What stands between this key and a working connection, if anything."""
    if not probe["configured"]:
        return f"Add a {network} API key: {variables}"
    if probe["unreachable"]:
        return f"Cannot reach Deribit: {probe['error']}"
    if not probe["authenticated"]:
        return f"Deribit or the connector refused the key: {probe['error']}"
    if probe["locked"]:
        return f"Deribit has ETH locked on {network}: wait for it to clear"
    if not probe["healthy"]:
        return f"Connection is unhealthy: {probe['error'] or 'see the server logs'}"
    return None


def _block_rfq(probe: dict[str, Any]) -> str | None:
    """The key's Block RFQ scopes, once it has authenticated."""
    missing = probe.get("block_rfq_missing")
    if missing is None:
        return None
    return "Block RFQ scopes granted" if not missing else "Block RFQ needs " + ", ".join(missing)


def _testnet_row(probe: dict[str, Any]) -> dict[str, Any]:
    test = TESTS["deribit-testnet"]
    tested = test["status"] == "passed"
    connection, connected = _connection(probe)
    step = _key_step(probe, "testnet",
                     "DERIBIT_TESTNET_CLIENT_ID and DERIBIT_TESTNET_CLIENT_SECRET")
    if step is None and not tested:
        step = "Run tests/test_deribit_testnet.py"
    return {
        "id": "deribit-testnet", "name": "Deribit ETH", "network": "Testnet",
        "connection": connection, "connected": connected,
        "tested": tested, "test_detail": test["detail"],
        "stage": "Tested on testnet" if tested and connected else "Not tested",
        "ready": tested and connected, "next_step": step,
        "block_rfq": _block_rfq(probe),
    }


def _production_row(probe: dict[str, Any]) -> dict[str, Any]:
    test = TESTS["deribit-production"]
    tested = test["status"] == "passed"
    connection, connected = _connection(probe)
    if TESTS["deribit-testnet"]["status"] != "passed":
        step = "Pass the testnet tests first"
    else:
        step = _key_step(probe, "production", "DERIBIT_CLIENT_ID and DERIBIT_CLIENT_SECRET, "
                                              "with DERIBIT_TESTNET=0")
        if step is None and not tested:
            step = "Run the checks with a trade-only production key"
        if step is None and not DERIBIT_CONNECTOR_PRODUCTION:
            step = "Set DERIBIT_CONNECTOR_PRODUCTION=1"
    ready = step is None
    return {
        "id": "deribit-production", "name": "Deribit ETH", "network": "Production",
        "connection": connection, "connected": connected,
        "tested": tested, "test_detail": test["detail"],
        "stage": "Ready in prod" if ready else "Not ready",
        "ready": ready, "next_step": step,
        "block_rfq": _block_rfq(probe),
    }


async def _probe(testnet: bool) -> dict[str, Any]:
    # A short-lived instance that only reads, so it needs no production opt-in.
    client_id, client_secret = deribit_keys(testnet)
    async with DeribitConnector(client_id, client_secret, testnet=testnet,
                                allow_production=not testnet) as connector:
        return await connector.diagnostics()


async def deribit_portfolio() -> dict[str, Any]:
    """The Deribit ETH account on the configured network, for Portfolio P&L. Read-only."""
    client_id, client_secret = deribit_keys(DERIBIT_TESTNET)
    out: dict[str, Any] = {"network": "Testnet" if DERIBIT_TESTNET else "Production",
                           "connected": False, "account": None, "positions": [], "error": None}
    if not client_id:
        return out | {"error": "No Deribit API key set up"}
    async with DeribitConnector(client_id, client_secret, testnet=DERIBIT_TESTNET,
                                allow_production=not DERIBIT_TESTNET) as connector:
        try:
            account = await connector.account()
            positions = await connector.positions()
        except ConnectorError as exc:
            return out | {"error": str(exc)}
    return out | {"connected": True, "account": asdict(account),
                  "positions": [asdict(p) for p in positions]}


async def connector_statuses() -> list[dict[str, Any]]:
    testnet, production = await asyncio.gather(_probe(True), _probe(False))
    return [_testnet_row(testnet), _production_row(production)]
