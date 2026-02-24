"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from plgo_options.web.routes import market, pricing, strategies

from plgo_options.web.routes import positions

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"


def create_app() -> FastAPI:
    app = FastAPI(
        title="PLGO Options — ETH Pricing",
        version="0.1.0",
        description="Price ETH options & strategies using live Deribit data",
    )

    # API routes
    app.include_router(market.router, prefix="/api/market", tags=["market"])
    app.include_router(pricing.router, prefix="/api/pricing", tags=["pricing"])
    app.include_router(strategies.router, prefix="/api/strategies", tags=["strategies"])
    app.include_router(positions.router, prefix="/api/positions", tags=["positions"])

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