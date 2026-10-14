"""Starred proposals (14 Oct 2026): kept across sweeps and always listed."""

import asyncio

from plgo_options.agents import store
from plgo_options.data import database
from plgo_options.web.routes import agents as routes


def test_starred_survive_supersede_and_listing(tmp_path):
    database._db = None
    database.DB_PATH = tmp_path / "star_test.db"

    async def go():
        try:
            await body()
        finally:
            await database.close_db()

    async def body():
        await store.init_agent_tables()
        await store.init_agent_tables()                      # the migration is idempotent
        a = await store.add_proposal("roll_sweep", "FIL", "roll pm #1", "a", "lucas", [], {})
        b = await store.add_proposal("roll_sweep", "FIL", "roll pm #2", "b", "lucas", [], {})
        await store.star_proposal(a, True)
        assert await store.supersede_open("FIL", "roll_sweep", []) == 1
        rows = {r["id"]: r for r in await store.list_proposals()}
        assert rows[a]["status"] == "open" and rows[a]["starred"] == 1
        assert rows[b]["status"] == "superseded" and rows[b]["starred"] == 0
        # Older than the page's limit: still returned, because it is starred.
        for i in range(3):
            await store.add_proposal("optimizer", "FIL", f"v4 am #{i}", "x", "lucas", [], {})
        ids = [r["id"] for r in (await routes.proposals(limit=2))["proposals"]]
        assert a in ids and b not in ids
        await store.star_proposal(a, False)
        assert a not in [r["id"] for r in await store.list_starred()]

    asyncio.run(go())
