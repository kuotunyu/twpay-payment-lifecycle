"""FastAPI app assembly.

Run with: uv run python -m uvicorn twpay_checkout.main:app --reload
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import Settings
from .db import build_engine, init_db
from .routes import callbacks, operations, orders, shop


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(app.state.engine)
        yield

    app = FastAPI(title="twpay-checkout", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = build_engine(settings.database_url)
    app.include_router(shop.router)
    app.include_router(orders.router)
    app.include_router(callbacks.router)
    app.include_router(operations.router)
    return app


app = create_app()
