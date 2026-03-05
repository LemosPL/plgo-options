"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

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

    # Static files (only mount if directory exists)
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # Templates
    templates = Jinja2Templates(directory=TEMPLATES_DIR)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse("index.html", {"request": request})

    return app


app = create_app()
