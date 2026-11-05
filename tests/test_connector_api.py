"""The connector over HTTP, on the simulated Deribit from test_deribit_connector."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_deribit_connector import PERP, FakeDeribit

from plgo_options.execution.deribit import ConnectorError, DeribitConnector
from plgo_options.web.routes import connector_api


@pytest.fixture
def fake() -> FakeDeribit:
    return FakeDeribit()


@pytest.fixture
def state():
    return {"kill": False, "testnet": True}


@pytest.fixture
def client(fake, state, monkeypatch) -> TestClient:
    conn = DeribitConnector("id", "secret", testnet=state["testnet"], allow_production=True,
                            transport=httpx.MockTransport(fake.handler),
                            clock=fake.clock, sleep=fake.clock_sleep)

    async def kill() -> bool:
        return state["kill"]

    monkeypatch.setattr(connector_api, "get_connector", lambda: conn)
    monkeypatch.setattr(connector_api, "kill_switch_on", kill)
    monkeypatch.setattr(connector_api, "current_user", lambda _r: {"email": "c@x.io"})
    app = FastAPI()
    app.include_router(connector_api.router, prefix="/api/connector")
    return TestClient(app)


def _order(**kw) -> dict:
    return {"intent_id": "a", "instrument": PERP, "side": "buy", "qty": 10,
            "limit_price": 2400.0} | kw


def _sent(fake: FakeDeribit) -> list:
    return [m for m, _ in fake.calls if m in ("private/buy", "private/sell")]


def test_catalog_lists_every_connector_call_and_each_route_exists(client):
    body = client.get("/api/connector/catalog").json()
    assert {c["name"] for c in body["calls"]} == {
        "submit", "cancel", "order", "book", "positions", "account", "healthy"}
    assert body["network"] == "Testnet" and body["kill_switch"] is False
    assert "get_connector" in body["python_example"]
    routes = {(m, r.path) for r in client.app.routes for m in getattr(r, "methods", [])}
    for call in body["calls"]:
        path = call["path"].replace("{instrument}", "{instrument:path}").replace(
            "{order_id}", "{order_id:path}")
        assert (call["method"], path) in routes, call["name"]


def test_reads(client, fake):
    assert client.get("/api/connector/healthy").json() == {"healthy": True}
    assert client.get(f"/api/connector/book/{PERP}").json()["best_bid"] == 2500.0
    assert client.get("/api/connector/account").json()["equity"] == 12.5
    assert client.get("/api/connector/positions").json() == {"positions": []}


def test_submit_track_and_cancel(client, fake):
    placed = client.post("/api/connector/orders", json=_order()).json()
    assert placed["status"] == "open" and placed["intent"]["proposal_id"] == "manual:c@x.io"
    oid = placed["order_id"]
    assert client.get(f"/api/connector/orders/{oid}").json()["status"] == "open"
    assert client.post(f"/api/connector/orders/{oid}/cancel").json()["status"] == "cancelled"


def test_same_intent_id_places_one_order(client, fake):
    first = client.post("/api/connector/orders", json=_order()).json()
    again = client.post("/api/connector/orders", json=_order()).json()
    assert first["order_id"] == again["order_id"] and len(fake.orders) == 1


def test_exchange_rejection_is_a_normal_response(client, fake):
    r = client.post("/api/connector/orders", json=_order(qty=2e7))
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert r.json()["reject_reason"] == "not_enough_funds"


def test_kill_switch_stops_submit_but_never_cancel(client, fake, state):
    oid = client.post("/api/connector/orders", json=_order()).json()["order_id"]
    state["kill"] = True
    r = client.post("/api/connector/orders", json=_order(intent_id="b"))
    assert r.status_code == 423 and len(_sent(fake)) == 1
    assert client.post(f"/api/connector/orders/{oid}/cancel").json()["status"] == "cancelled"


def test_unhealthy_connector_sends_nothing(client, fake):
    fake.locked = {"locked": True, "locked_indices": ["eth_usd"]}
    r = client.post("/api/connector/orders", json=_order())
    assert r.status_code == 503 and _sent(fake) == []


@pytest.mark.parametrize("state", [{"kill": False, "testnet": False}])
def test_production_orders_stay_off_until_tests_pass(client, fake, state, monkeypatch):
    r = client.post("/api/connector/orders", json=_order())
    assert r.status_code == 403 and _sent(fake) == []
    monkeypatch.setitem(connector_api.TESTS, "deribit-production",
                        {"status": "passed", "detail": ""})
    assert client.post("/api/connector/orders", json=_order()).status_code == 200


def test_malformed_order_is_refused_before_it_is_sent(client, fake):
    r = client.post("/api/connector/orders", json=_order(limit_price=None))
    assert r.status_code == 422 and "limit_price" in r.json()["detail"] and _sent(fake) == []
    assert client.post("/api/connector/orders", json=_order(side="hold")).status_code == 422


def test_callers_mistake_is_400_and_connector_trouble_is_503(client, fake):
    assert client.get("/api/connector/orders/nope").status_code == 400
    fake.fail_next["private/get_account_summary"] = (10028, "too_many_requests")
    assert client.get("/api/connector/account").status_code == 503


def test_connector_refused_is_a_clear_503(client, monkeypatch):
    def refused():
        raise ConnectorError("refusing production")

    monkeypatch.setattr(connector_api, "get_connector", refused)
    r = client.get("/api/connector/positions")
    assert r.status_code == 503 and r.json()["detail"] == "refusing production"
    assert "not switched on" in client.get("/api/connector/catalog").json()["network"]
