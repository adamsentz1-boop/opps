"""Database engine and session management (SQLite via SQLAlchemy)."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        url = get_settings().database_url
        if url.startswith("sqlite:///"):
            db_path = url.replace("sqlite:///", "", 1)
            if db_path and db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args, future=True)
        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def _set_sqlite_pragma(dbapi_connection, _record):  # pragma: no cover - trivial
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False, future=True)
    return _engine


def get_sessionmaker() -> sessionmaker:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def ensure_columns() -> list[str]:
    """Add columns that exist on the models but not yet in an existing SQLite database.

    `create_all` creates missing tables but never alters existing ones, so a release that adds a column to a
    table someone already has would fail at query time. This closes that gap for the only case SQLite can do
    safely and non-destructively: ADD COLUMN.

    Deliberately additive only. Nothing is dropped, renamed or retyped, and a NOT NULL column without a
    server default is skipped with a warning rather than guessed at, because SQLite cannot backfill one.
    Real migrations still belong in Alembic once the schema settles; this keeps existing databases working
    in the meantime. Returns the list of applied changes.
    """
    from sqlalchemy import inspect, text

    engine = get_engine()
    if not engine.url.get_backend_name().startswith("sqlite"):
        return []      # other backends get real migrations, not this
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    applied: list[str] = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue      # create_all handles brand new tables
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                if not column.nullable and column.server_default is None:
                    log.warning("cannot add NOT NULL column %s.%s without a server default; skipping",
                                table.name, column.name)
                    continue
                ddl_type = column.type.compile(engine.dialect)
                sql = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}'
                if column.server_default is not None:
                    default = getattr(column.server_default, "arg", column.server_default)
                    sql += f" DEFAULT {default if isinstance(default, str) else default.text}"
                conn.execute(text(sql))
                applied.append(f"{table.name}.{column.name}")
    if applied:
        log.info("schema: added missing column(s) %s", ", ".join(applied))
    return applied


def init_db() -> None:
    """Create all tables, add any newly-introduced columns, and seed default settings."""
    from app import models  # noqa: F401  (register models)
    from app.settings_service import seed_default_settings

    Base.metadata.create_all(bind=get_engine())
    ensure_columns()
    with session_scope() as db:
        seed_default_settings(db)
        if get_settings().market_challenge_enabled:
            from app.market.portfolio import get_or_create_challenge
            get_or_create_challenge(db)   # the watchlist is deliberately seeded EMPTY


def reset_engine_for_tests(url: str) -> None:
    """Point the engine at a fresh database (used by the test-suite)."""
    global _engine, _SessionLocal
    get_settings.cache_clear()
    import os
    os.environ["DATABASE_URL"] = url
    _engine = None
    _SessionLocal = None


@contextmanager
def session_scope() -> Iterator[Session]:
    db = get_sessionmaker()()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = get_sessionmaker()()
    try:
        yield db
    finally:
        db.close()
