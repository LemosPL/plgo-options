"""The agents page chooses which counterparties the optimizer sweep may trade with."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from plgo_options.agents.policy import default_policy
from plgo_options.web.routes import agents as route


@pytest.fixture
def env(monkeypatch):
    pol = default_policy("FIL")
    saved = []

    async def book(asset):
        return ["FalconX", "Flowdesk", "G20", "KeyRock"]

    async def get_policy(asset):
        return pol

    async def save_policy(p, by=""):
        saved.append((list(p.optimizer.counterparties), by))
        return p

    monkeypatch.setattr(route, "book_counterparties", book)
    monkeypatch.setattr(route.store, "get_policy", get_policy)
    monkeypatch.setattr(route.store, "save_policy", save_policy)
    return pol, saved


@pytest.mark.asyncio
async def test_removing_counterparties_saves_the_rest(env):
    pol, saved = env
    out = await route.set_sweep_counterparties("fil", route.CptyReq(counterparties=["flowdesk", "KeyRock"]))
    assert out["selected"] == ["Flowdesk", "KeyRock"]
    assert pol.optimizer.counterparties == ["Flowdesk", "KeyRock"] and saved[0][0] == ["Flowdesk", "KeyRock"]


@pytest.mark.asyncio
async def test_ticking_every_counterparty_means_all(env):
    pol, _ = env
    out = await route.set_sweep_counterparties("FIL", route.CptyReq(
        counterparties=["FalconX", "Flowdesk", "G20", "KeyRock"]))
    assert out["selected"] == [] and pol.optimizer.counterparties == []


@pytest.mark.asyncio
async def test_a_counterparty_not_in_the_book_is_refused(env):
    with pytest.raises(HTTPException):
        await route.set_sweep_counterparties("FIL", route.CptyReq(counterparties=["Nobody"]))
