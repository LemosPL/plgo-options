"""FastAPI application factory."""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager

# Force UTF-8 on the process's own log streams before anything can write to
# them. The optimizer routinely prints diagnostics containing mathematical
# symbols ("existing_qty≠0", "σ", "Δ"), and on Windows stdout defaults to
# cp1252 — so a plain print() inside run_lp raised UnicodeEncodeError and took
# down the entire optimizer request with a bare 500. Log formatting must never
# be able to fail a run; errors="replace" guarantees it can't. No-op on Cloud
# Run, which is already UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass  # a stream that refuses reconfiguration is not worth failing over

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from plgo_options.data.database import init_db, close_db
from plgo_options.web.routes import market, pricing, strategies
from plgo_options.web.routes import positions
from plgo_options.web.routes import portfolio
from plgo_options.web.routes import trades
from plgo_options.web.routes import optimization
from plgo_options.web.routes import optimizer
from plgo_options.web.routes import execution
from plgo_options.web.routes import holistic
from plgo_options.web.routes import collateral
from plgo_options.web.routes import reconciliation
from plgo_options.web.routes import deals
from plgo_options.web.routes import signals

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    await close_db()


def create_app() -> FastAPI:
    app = FastAPI(
        title="PLGO Options — ETH Pricing",
        version="0.1.0",
        description="Price ETH options & strategies using live Deribit data",
        lifespan=lifespan,
    )

    # API routes
    app.include_router(market.router, prefix="/api/market", tags=["market"])
    app.include_router(pricing.router, prefix="/api/pricing", tags=["pricing"])
    app.include_router(strategies.router, prefix="/api/strategies", tags=["strategies"])
    app.include_router(positions.router, prefix="/api/positions", tags=["positions"])
    app.include_router(portfolio.router, prefix="/api/portfolio", tags=["portfolio"])
    app.include_router(trades.router, prefix="/api/trades", tags=["trades"])
    app.include_router(optimization.router, prefix="/api/optimization", tags=["optimization"])
    app.include_router(optimizer.router, prefix="/api/optimizer", tags=["optimizer"])
    app.include_router(execution.router, prefix="/api/execution", tags=["execution"])
    app.include_router(holistic.router, prefix="/api/holistic", tags=["holistic"])
    app.include_router(collateral.router, prefix="/api/collateral", tags=["collateral"])
    app.include_router(reconciliation.router, prefix="/api/reconciliation", tags=["reconciliation"])
    app.include_router(deals.router, prefix="/api/deals", tags=["deals"])
    app.include_router(signals.router, prefix="/api/signals", tags=["signals"])

    # Static files (only mount if directory exists)
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # Templates
    templates = Jinja2Templates(directory=TEMPLATES_DIR)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(request=request, name="index.html")

    return app


app = create_app()
