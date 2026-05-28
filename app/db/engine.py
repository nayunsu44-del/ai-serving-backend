from __future__ import annotations

from pathlib import Path

from fastapi import Request
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


def ensure_sqlite_parent_dir(database_url: str) -> None:
    if not database_url.startswith("sqlite+aiosqlite:///"):
        return

    path = database_url.removeprefix("sqlite+aiosqlite:///")
    if path == ":memory:":
        return

    Path(path).parent.mkdir(parents=True, exist_ok=True)


def create_engine(database_url: str) -> AsyncEngine:
    ensure_sqlite_parent_dir(database_url)
    kwargs = {}
    if database_url == "sqlite+aiosqlite:///:memory:":
        kwargs["poolclass"] = StaticPool
    engine = create_async_engine(database_url, **kwargs)

    if database_url.startswith("sqlite+aiosqlite:///") and database_url != (
        "sqlite+aiosqlite:///:memory:"
    ):

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    return engine


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def get_sessionmaker(request: Request) -> async_sessionmaker[AsyncSession] | None:
    return getattr(request.app.state, "db_sessionmaker", None)
