"""Quote recorder endpoints (market_data/quote_recorder.py).

Scheduler-facing (guarded by X-Signals-Token):
    POST /api/quote-recorder/tick?until=YYYY-MM-DD   hold the quote feed 55 s, write one file;
                                                     after ``until`` (UTC) it records nothing

People-facing:
    GET  /api/quote-recorder/status                  files and MB per day, the root
    GET  /api/quote-recorder/day/{day}               one UTC day's files as a zip
"""

from __future__ import annotations

import datetime as dt
import io
import re
import zipfile

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse

from plgo_options.config import SIGNALS_TOKEN
from plgo_options.market_data import quote_recorder as rec

router = APIRouter()
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _require_token(token: str | None) -> None:
    if not SIGNALS_TOKEN:
        return
    if (token or "") != SIGNALS_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Signals-Token")


@router.post("/tick")
async def tick(until: str | None = None, seconds: float = 55.0, expiries: int = 3,
               x_signals_token: str | None = Header(default=None)):
    _require_token(x_signals_token)
    if until is not None and not DAY.fullmatch(until):
        raise HTTPException(status_code=400, detail="until must be YYYY-MM-DD")
    if until is not None and dt.datetime.now(dt.timezone.utc).date().isoformat() > until:
        return {"skipped": f"recording ended on {until}"}
    try:
        return await rec.record(seconds=min(seconds, 58.0), n_expiries=expiries)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"quote recorder: {type(e).__name__}: {e}")


@router.get("/status")
async def status():
    per_day = rec.days()
    return {"root": str(rec.data_root()), "days": per_day,
            "files": sum(d["files"] for d in per_day.values()),
            "MB": round(sum(d["MB"] for d in per_day.values()), 1)}


@router.get("/day/{day}")
async def day_zip(day: str):
    if not DAY.fullmatch(day):
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD")
    files = sorted((rec.data_root() / day).glob("*.csv.gz"))
    if not files:
        raise HTTPException(status_code=404, detail=f"nothing recorded on {day}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:  # already gzipped
        for f in files:
            z.write(f, f"{day}/{f.name}")
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="quote_recorder_{day}.zip"'})
