from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    database_url = url or get_settings().resolved_database_url
    kwargs = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in database_url:
            kwargs["poolclass"] = StaticPool
    return create_engine(database_url, **kwargs)


engine = None
SessionLocal = None


def init_database(url: str | None = None) -> None:
    global engine, SessionLocal
    engine = make_engine(url)
    SessionLocal = sessionmaker(engine, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    if SessionLocal is None:
        init_database()
    assert SessionLocal is not None
    with SessionLocal() as session:
        yield session


def database_ready() -> bool:
    try:
        if engine is None:
            init_database()
        assert engine is not None
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
