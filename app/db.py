"""
Database connection, engine configuration, and session lifecycle.
"""

from __future__ import annotations

from typing import Generator
from sqlmodel import SQLModel, Session, create_engine
from app.config import settings


def get_engine(db_url: str | None = None):
    url = db_url or settings.normalized_database_url
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(url, echo=False, connect_args=connect_args)


engine = get_engine()


def init_db(target_engine=None) -> None:
    """Initialize database tables."""
    import app.models  # noqa: F401 - ensure models are registered on metadata
    eng = target_engine or engine
    SQLModel.metadata.create_all(eng)


def get_session() -> Generator[Session, None, None]:
    """Dependency for yielding an active SQLModel database session."""
    with Session(engine, expire_on_commit=False) as session:
        yield session
