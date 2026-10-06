"""Desk agents API.

Scheduler-facing (guarded by X-Signals-Token, the same secret as the brief):
    POST /api/agents/run/{name}        run one agent; Cloud Scheduler calls these

People-facing (the Agents tab):
    GET  /api/agents/status            policies, flags, last run of each agent
    GET  /api/agents/runs              run history (text of every brief)
    GET  /api/agents/proposals         proposals and where the gate routed them
    GET  /api/agents/proposals/{id}/v4 v4 run parameters + designed legs, for "Validate in v4"
    POST /api/agents/proposals/{id}    mark executed / declined (records who)
    GET  /api/agents/policy/{asset}    Monday's settings
    PUT  /api/agents/policy/{asset}    save Monday's settings (floor only goes up)
    POST /api/agents/flags             kill switch, curve freeze
    POST /api/agents/gate              dry-run any proposal through the gate
    POST /api/agents/optimizer/sweep   run the v4 sweep now, return the ranking
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from plgo_options.agents import desk, gate as gate_mod, optimizer as opt_mod, store
from plgo_options.agents.policy import AssetPolicy
from plgo_options.config import SIGNALS_TOKEN

router = APIRouter()


def _require_token(token: str | None) -> None:
    if not SIGNALS_TOKEN:
        return
    if (token or "") != SIGNALS_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Signals-Token")


class RunRequest(BaseModel):
    deliver: bool = True
    use_ai: bool = True
    ctx: dict[str, Any] | None = None


@router.post("/run/{name}")
async def run(name: str, req: RunRequest | None = None,
              x_signals_token: str | None = Header(default=None)):
    req = req or RunRequest()
    if req.deliver:
        _require_token(x_signals_token)
    if name not in desk.AGENTS:
        raise HTTPException(status_code=404, detail=f"Unknown agent '{name}'. Known: {sorted(desk.AGENTS)}")
    try:
        return await desk.run_agent(name, req.ctx, deliver=req.deliver, use_ai=req.use_ai)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{name} failed: {type(e).__name__}: {e}")


@router.get("/status")
async def status():
    last = {}
    for name in desk.AGENTS:
        r = await store.last_run(name)
        last[name] = ({"id": r["id"], "started_at": r["started_at"], "status": r["status"],
                       "delivered": bool(r["delivered"])} if r else None)
    return {
        "kill_switch": await store.get_flag("kill_switch", "off"),
        "curves_frozen": {a: await store.get_flag(f"curves_frozen_{a}", "off") for a in desk.ASSETS},
        "policies": {a: (await store.get_policy(a)).to_dict() for a in desk.ASSETS},
        "agents": last,
        "open_proposals": len(await store.list_proposals("open")),
    }


@router.get("/runs")
async def runs(agent: str | None = None, limit: int = 30):
    return {"runs": await store.list_runs(agent, limit)}


@router.get("/proposals")
async def proposals(status: str | None = None, limit: int = 100):
    return {"proposals": await store.list_proposals(status, limit)}


@router.get("/proposals/{pid}/v4")
async def proposal_v4(pid: int):
    """Everything the v4 page needs to replay a proposal: the run parameters
    (stored on optimizer proposals, else rebuilt from today's policy preset)
    and the legs the agent designed, so the page can show both."""
    row = await store.get_proposal(pid)
    if not row:
        raise HTTPException(status_code=404, detail=f"No proposal #{pid}")
    pol = await store.get_policy(row["asset"])
    p = row["proposal"] or {}
    params = p.get("v4_params")
    variant, source = opt_mod.variant_for(row, pol.optimizer)
    if params:
        source = "stored"
    else:
        params = opt_mod.run_kwargs(row["asset"], pol.optimizer, variant)
    return {"id": pid, "asset": row["asset"], "agent": row["agent"], "kind": row["kind"],
            "summary": row["summary"], "route": row["route"], "reasons": row["reasons"],
            "params": params, "variant": variant, "params_source": source,
            "legs": p.get("legs") or [], "net_cost_usd": p.get("net_cost_usd"),
            "spot": p.get("spot")}


class Decision(BaseModel):
    status: str             # executed | declined | open
    by: str = ""


@router.post("/proposals/{pid}")
async def decide(pid: int, d: Decision):
    if d.status not in ("executed", "declined", "open"):
        raise HTTPException(status_code=400, detail="status must be executed, declined or open")
    await store.decide_proposal(pid, d.status, d.by)
    return {"ok": True}


@router.get("/policy/{asset}")
async def get_policy(asset: str):
    return (await store.get_policy(asset)).to_dict()


@router.put("/policy/{asset}")
async def put_policy(asset: str, body: dict[str, Any], by: str = ""):
    body = {**body, "asset": asset.upper()}
    try:
        pol = AssetPolicy.from_dict(body)
        pol.is_example = bool(body.get("is_example", False))
        await store.save_policy(pol, by)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return pol.to_dict()


class FlagReq(BaseModel):
    key: str                # kill_switch | curves_frozen_ETH | curves_frozen_FIL
    value: str              # on | off
    by: str = ""


@router.post("/flags")
async def flags(req: FlagReq):
    if not (req.key == "kill_switch" or req.key.startswith("curves_frozen_")):
        raise HTTPException(status_code=400, detail="unknown flag")
    if req.value not in ("on", "off"):
        raise HTTPException(status_code=400, detail="value must be on or off")
    await store.set_flag(req.key, req.value, req.by)
    return {"ok": True, req.key: req.value}


@router.post("/gate")
async def gate(proposal: dict[str, Any]):
    asset = str(proposal.get("asset") or "ETH").upper()
    pol = await store.get_policy(asset)
    spot = float(proposal.get("spot") or 0) or (await desk.get_spot(asset)) or 0
    ctx = await desk._gate_ctx(pol, spot)
    return gate_mod.evaluate(proposal, ctx).to_dict()


class SweepReq(BaseModel):
    asset: str = "ETH"
    variants: list[dict[str, Any]] | None = None
    custom_spot: float | None = None


@router.post("/optimizer/sweep")
async def sweep(req: SweepReq):
    pol = await store.get_policy(req.asset)
    res = await opt_mod.sweep(pol, req.custom_spot, req.variants)
    slim = lambda s: {k: s[k] for k in ("variant", "fit_gain_pct", "cost_usd", "net_premium_usd",
                                        "option_lines", "key_spot_changes", "score", "disqualified")}
    return {**{k: res[k] for k in ("asset", "target", "runs", "errors", "spot", "book_mtm")},
            "ranked": [slim(s) for s in res["ranked"]], "pareto": [slim(s) for s in res["pareto"]],
            "best_trades": res["best"][0]["trades"] if res["best"] else []}
