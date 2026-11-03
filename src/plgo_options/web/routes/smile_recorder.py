"""Smile recorder endpoints (market_data/smile_recorder.py).

Scheduler-facing (guarded by X-Signals-Token):
    POST /api/smile-recorder/tick        snapshot + fit the next ETH quarterlies, write one tick

People-facing:
    GET  /api/smile-recorder/status      ticks per day, the last tick, edge summary
    GET  /api/smile-recorder/day/{day}   one UTC day's tick files as a zip, for local analysis
"""

from __future__ import annotations

import io
import re
import zipfile

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse

from plgo_options.config import SIGNALS_TOKEN
from plgo_options.market_data import smile_recorder as rec

router = APIRouter()


def _require_token(token: str | None) -> None:
    if not SIGNALS_TOKEN:
        return
    if (token or "") != SIGNALS_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Signals-Token")


@router.post("/tick")
async def tick(expiries: int = 3, depth: int = 10, x_signals_token: str | None = Header(default=None)):
    _require_token(x_signals_token)
    try:
        return await rec.record_tick(n_expiries=expiries, depth=depth)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"smile recorder: {type(e).__name__}: {e}")


@router.get("/status")
async def status():
    per_day = rec.days()
    last = None
    if per_day:
        day = max(per_day)
        _, fits = rec.load(day=day)
        if not fits.empty:
            last = fits[fits.ts == fits.ts.max()].to_dict("records")
    strikes, _ = rec.load()
    return {"root": str(rec.data_root()), "days": per_day, "ticks": sum(per_day.values()),
            "last": last, "summary": rec.summarize(strikes)}


@router.get("/day/{day}")
async def day_zip(day: str):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD")
    files = sorted((rec.data_root() / day).glob("*.json.gz"))
    if not files:
        raise HTTPException(status_code=404, detail=f"nothing recorded on {day}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:  # already gzipped
        for f in files:
            z.write(f, f"{day}/{f.name}")
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="smile_recorder_{day}.zip"'})
