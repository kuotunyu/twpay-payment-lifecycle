"""FastAPI dependencies bound to the app instance (overridable in tests)."""
from collections.abc import Iterator
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from .config import Settings

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_session(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as session:
        yield session
