"""Where every connector stands, for the Connectors screen.

Connection is checked live on each call, read-only. Test results can't be
measured from a running server, so ``TESTS`` is a record: change a connector to
"passed" only after its suite has really been run against that network.
A connector is only ever called ready when both agree.
"""

from __future__ import annotations

import asyncio
from typing import Any

from plgo_options.config import DERIBIT_CONNECTOR_PRODUCTION

from .deribit import DeribitConnector, deribit_keys

TESTS: dict[str, dict[str, str]] = {
    "paper": {"status": "passed", "detail": "tests/test_execution.py, offline"},
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


def _paper_row() -> dict[str, Any]:
    return {
        "id": "paper", "name": "Paper", "network": "Simulated fills",
        "connection": "No connection needed", "connected": True,
        "tested": TESTS["paper"]["status"] == "passed", "test_detail": TESTS["paper"]["detail"],
        "stage": "Simulation only", "ready": False, "next_step": None,
    }


def _testnet_row(probe: dict[str, Any]) -> dict[str, Any]:
    test = TESTS["deribit-testnet"]
    tested = test["status"] == "passed"
    connection, connected = _connection(probe)
    step = _key_step(probe, "testnet",
                     "DERIBIT_TESTNET_CLIENT_ID and DERIBIT_TESTNET_CLIENT_SECRET")
    if step is None and not tested:
        step = "Run tests/test_deribit_testnet.py"
    return {
        "id": "deribit-testnet", "name": "Deribit", "network": "Testnet",
        "connection": connection, "connected": connected,
        "tested": tested, "test_detail": test["detail"],
        "stage": "Tested on testnet" if tested and connected else "Not tested",
        "ready": tested and connected, "next_step": step,
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
        "id": "deribit-production", "name": "Deribit", "network": "Production",
        "connection": connection, "connected": connected,
        "tested": tested, "test_detail": test["detail"],
        "stage": "Ready in prod" if ready else "Not ready",
        "ready": ready, "next_step": step,
    }


async def _probe(testnet: bool) -> dict[str, Any]:
    # A short-lived instance that only reads, so it needs no production opt-in.
    client_id, client_secret = deribit_keys(testnet)
    async with DeribitConnector(client_id, client_secret, testnet=testnet,
                                allow_production=not testnet) as connector:
        return await connector.diagnostics()


async def connector_statuses() -> list[dict[str, Any]]:
    testnet, production = await asyncio.gather(_probe(True), _probe(False))
    return [_paper_row(), _testnet_row(testnet), _production_row(production)]
