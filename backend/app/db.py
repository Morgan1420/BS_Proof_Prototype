"""SQLite database setup via SQLModel.

Note on the engine URL: the task spec calls for `sqlite:///./data/app.db`
(relative to the process's current working directory). We instead resolve
`data/app.db` to an absolute path anchored to `backend/`, for the same
reason `app/core/config.py` resolves `.env` by absolute path — the app
should behave identically whether uvicorn is launched from `backend/` or
from somewhere else. The database file still ends up at
`backend/data/app.db`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

# Imported for its side effect: registers Product/Ingredient on
# SQLModel.metadata so init_db()'s create_all() knows about them. Safe at
# module load time — app.models.supplement has no dependency on app.db.
from app.models import supplement  # noqa: F401

# backend/app/db.py -> parents[1] == backend/
_BACKEND_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = _BACKEND_DIR / "data"
DATABASE_PATH = DATA_DIR / "app.db"
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"

# check_same_thread=False: FastAPI runs sync dependencies/functions (like
# get_session below, and the storage.save_scan() calls made through it) in
# a worker threadpool, which may differ from the thread that opened the
# connection. SQLite disallows cross-thread connection use by default;
# this opts back in. Fine for this single-process dev/prototype setup.
engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:  # noqa: ANN001, ARG001
    """pysqlite ships with FK enforcement OFF by default, per-connection.

    Without this, SQLite silently allows deleting a Product/Ingredient row
    that still has ProductIngredientLink rows pointing at it — it doesn't
    error, it just leaves orphaned links behind. This turns enforcement on
    for every new DBAPI connection the pool opens, which is also why
    storage.delete_all_data() below deletes in dependency order
    (links -> products -> ingredients) rather than relying on cascade.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def init_db() -> None:
    """Creates backend/data/ (if missing) and all registered tables.

    Call once at application startup (see app/main.py's lifespan handler).
    Safe to call repeatedly — `create_all` only creates tables that don't
    already exist. Non-destructive: existing data is left alone.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine)


def reset_database() -> None:
    """Destructively wipes the SQLite database file and recreates it from
    the current schema.

    This is a one-time MIGRATION utility for moving between schema
    versions (drops and recreates the file from scratch) — it is NOT what
    the runtime `DELETE /api/v1/dev/mock-data` endpoint calls day-to-day
    (that does a full, unconditional wipe of all rows while preserving
    the schema; see app/services/storage.py::delete_all_data). Run this
    manually once, e.g. from backend/, if you need to clear out records
    left over from a previous schema version:

        python -c "from app.db import reset_database; reset_database()"

    Deletes backend/data/app.db outright rather than using
    `SQLModel.metadata.drop_all()`. drop_all() only drops tables it
    currently recognizes by name, and this refactor renamed
    'product'/'ingredient' to 'products'/'ingredients' and added
    'product_ingredient_links' — so drop_all() would silently leave the
    old, now-orphaned tables sitting in the file. Deleting the file
    guarantees a genuinely clean slate.
    """
    engine.dispose()  # close any open connections before removing the file
    if DATABASE_PATH.exists():
        DATABASE_PATH.unlink()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: yields a database session scoped to one request.

    Usage: `session: Session = Depends(get_session)` in a route.
    """
    with Session(engine) as session:
        yield session
