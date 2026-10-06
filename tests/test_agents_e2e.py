"""End-to-end: the manual's example week, run through the real agents and DB.

Market data is stubbed (spot, book, perp, collateral, orders) so the test is
deterministic and runs offline; everything else - policy, row fires, the gate,
proposals, run records - is the production code path.
"""

import asyncio

from plgo_options.agents import desk, store
from plgo_options.agents.policy import default_policy
from plgo_options.data import database


def setup_db(tmp_path):
    database._db = None
    database.DB_PATH = tmp_path / "agents_test.db"


def stub(monkeypatch, spot_box):
    async def spot(asset):
        return spot_box[asset]

    async def book(asset):
        s = spot_box[asset]
        calls = [{"opt": "C", "qty": 2500, "strike": 2800, "expiry": "2026-12-25", "counterparty": "Flowdesk"},
                 {"opt": "C", "qty": 2500, "strike": 3000, "expiry": "2026-12-25", "counterparty": "Flowdesk"},
                 {"opt": "P", "qty": 2000, "strike": 2700, "expiry": "2026-12-25", "counterparty": "Flowdesk"}]
        return {"spot": s, "mtm": -22_500_000.0, "delta": 2285.0, "theta": -11_800.0, "vega": 40_000.0,
                "positions": calls, "n": len(calls)}

    async def perp(asset):
        return {"venue": "Binance Futures", "net_qty": 0, "notional_usd": 0,
                "funding": {"last_30d_usd": 0}, "market_error": None}

    async def collateral():
        return {"rows": [], "totals": {"shortfall_haircut": 0, "liability_usd": 33_900_000}}

    async def orders():
        return {"orders": []}

    monkeypatch.setattr(desk, "get_spot", spot)
    monkeypatch.setattr(desk, "get_book", book)
    monkeypatch.setattr(desk, "get_perp", perp)
    monkeypatch.setattr(desk, "get_collateral", collateral)
    monkeypatch.setattr(desk, "get_open_orders", orders)

    # Pricing each trade needs the full engine book; that pass has its own
    # tests (test_validate.py). Here it is skipped so the week runs offline.
    async def no_validation(prop, pol, book_payload=None):
        return prop
    monkeypatch.setattr(desk, "_validated", no_validation)


def test_example_week(tmp_path, monkeypatch):
    setup_db(tmp_path)
    spot = {"ETH": 3000.0, "FIL": 2.0}
    stub(monkeypatch, spot)

    async def go():
        await database.init_db()
        await store.init_agent_tables()
        for a, ref in (("ETH", 3000.0), ("FIL", 2.0)):
            pol = default_policy(a)
            pol.reference_price, pol.is_example = ref, False
            await store.save_policy(pol, "test")

        run = lambda name: desk.run_agent(name, deliver=False, use_ai=False)

        # Tuesday: ETH 3,310 -> +10% row fires, perp, Lucas alone.
        spot["ETH"] = 3310.0
        r = await run("row-watcher")
        assert "ROW +10 FIRED" in r["text"] and "LUCAS" in r["text"]

        # Wednesday: back to 3,050 -> nothing (row already fired this week).
        spot["ETH"] = 3050.0
        r = await run("row-watcher")
        assert "FIRED" not in r["text"]

        # Thursday: 3,620 -> +20% row: sell 25% of ITM calls, buy put spread; Lucas acts, tells Chris.
        spot["ETH"] = 3620.0
        r = await run("row-watcher")
        assert "ROW +20 FIRED" in r["text"] and "LUCAS_TELL_CHRIS" in r["text"]
        again = await run("row-watcher")
        assert "FIRED" not in again["text"]                       # once per week

        props = await store.list_proposals()
        assert [p["kind"] for p in props] == ["row +20", "row +10"]
        plus10 = props[1]["proposal"]
        assert plus10["legs"][0]["kind"] == "perp" and plus10["legs"][0]["side"] == "Sell"

        # FIL falls through the stop: wake people.
        spot["ETH"], spot["FIL"] = 3500.0, 1.05
        r = await run("row-watcher")
        assert r["wake"] and "STOP" in r["wake"][0]

        # The briefs run with data.
        for name in ("morning-open", "handover", "night-desk", "close-check", "monday-pack", "monthly-review"):
            r = await run(name)
            assert r["text"], name
        mo = (await store.last_run("morning-open"))["text"]
        assert "Fired this week: +10, +20" in mo
        ho = (await store.last_run("handover"))["text"]
        assert "4. Waiting for Chris:" in ho and "#2" in ho

        # People mark a proposal executed; it counts toward the month's cost.
        await store.decide_proposal(props[1]["id"], "executed", "lucas")
        assert (await store.list_proposals("executed"))[0]["decided_by"] == "lucas"

        # Kill switch: agents skip.
        await store.set_flag("kill_switch", "on", "test")
        r = await run("row-watcher")
        assert "Kill switch is ON" in r["text"]

        # The floor only goes up.
        pol = await store.get_policy("ETH")
        pol.floor_price = 3200
        await store.save_policy(pol, "test")
        pol.floor_price = 2700
        try:
            await store.save_policy(pol, "test")
            raise AssertionError("floor went down")
        except ValueError:
            pass
        await database.close_db()

    asyncio.run(go())
