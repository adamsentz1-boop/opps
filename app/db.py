"""Database engine and session management (SQLite via SQLAlchemy)."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


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


def init_db() -> None:
    """Create all tables and seed default settings."""
    from app import models  # noqa: F401  (register models)
    from app.settings_service import seed_default_settings

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    _add_missing_columns(engine)
    with session_scope() as db:
        seed_default_settings(db)


def _add_missing_columns(engine) -> None:
    """Tiny forward-only migration: add columns that exist in the models but not in the SQLite tables."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in inspector.get_table_names():
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                default = ""
                if column.default is not None and getattr(column.default, "arg", None) is not None \
                        and not callable(column.default.arg):
                    arg = column.default.arg
                    default = f" DEFAULT {repr(arg) if isinstance(arg, str) else int(arg) if isinstance(arg, bool) else arg}"
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}{default}'))
                if col_type.upper() == "JSON" and column.default is not None and callable(column.default.arg):
                    empty = "[]" if column.default.arg is list else "{}" if column.default.arg is dict else None
                    if empty is not None:
                        conn.execute(text(f'UPDATE "{table.name}" SET "{column.name}" = :v WHERE "{column.name}" IS NULL'),
                                     {"v": empty})


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
