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

# Same reasoning as above, plus one more: this also makes the "ResearchPaper"
# string forward-reference on Ingredient.papers (see
# app/models/supplement.py) resolvable — SQLAlchemy needs the actual
# ResearchPaper class registered on the shared SQLModel registry before it
# can configure that relationship, which happens lazily on first ORM use.
# app.models.research itself imports app.models.supplement (one-directional,
# no cycle), so this line alone is sufficient to register both.
from app.models import research  # noqa: F401

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


# Columns added to `Ingredient` (app/models/supplement.py) after the
# table already existed in deployed databases — see
# _migrate_ingredient_grading_columns() below for why these need special
# handling beyond create_all(). Each tuple is (column name, SQLite column
# DDL fragment including a DEFAULT, since SQLite requires one when adding
# a NOT NULL column to a non-empty table).
_INGREDIENT_GRADING_COLUMNS: tuple[tuple[str, str], ...] = (
    ("is_graded", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("grade_badge_text", "VARCHAR"),
)


def _migrate_ingredient_grading_columns() -> None:
    """Additive, idempotent migration: adds `is_graded`/`grade_badge_text`
    to an existing `ingredients` table if they're missing.

    `SQLModel.metadata.create_all()` (called just before this, in
    init_db()) only creates tables that don't exist *by name* yet — it
    never alters the columns of a table that's already there. The Phase 2
    grading feature added `is_graded`/`grade_badge_text` to the
    `Ingredient` model after `ingredients` already existed in any
    database created before that change, so `create_all()` alone leaves
    those databases schema-stale: every query touching `Ingredient`
    (e.g. GET /api/v1/supplements/search) fails with `sqlite3.
    OperationalError: no such column: ingredients.is_graded` even though
    the code expects the column to be there.

    This patches exactly that gap via `ALTER TABLE ... ADD COLUMN`,
    without needing a full `reset_database()` (which deletes the entire
    file, wiping every scanned Product/Ingredient). Safe to run on every
    startup — it checks `PRAGMA table_info` first and only adds a column
    if it's actually missing, so it's a no-op on a database that's
    already up to date (including a freshly-created one, where
    create_all() above already included these columns from the start).
    """
    if "ingredients" not in SQLModel.metadata.tables:
        return  # shouldn't happen — defensive, in case of a future rename

    with engine.connect() as connection:
        existing_columns = {
            row[1]  # PRAGMA table_info columns: (cid, name, type, notnull, dflt_value, pk)
            for row in connection.exec_driver_sql("PRAGMA table_info(ingredients)")
        }
        if not existing_columns:
            return  # table doesn't exist yet somehow — create_all() above should prevent this

        for column_name, column_ddl in _INGREDIENT_GRADING_COLUMNS:
            if column_name in existing_columns:
                continue
            connection.exec_driver_sql(
                f"ALTER TABLE ingredients ADD COLUMN {column_name} {column_ddl}"
            )
        connection.commit()


# Same pattern as _INGREDIENT_GRADING_COLUMNS above: `keywords`, then
# later `grade`/`grade_score`/`rubric_evaluation` (Phase 3 automated
# paper grading — see app/services/paper_grader.py) were added to
# `ResearchPaper` (app/models/research.py) after `research_papers`
# already existed in deployed databases. All nullable, so no DEFAULT is
# needed even on a non-empty table.
_RESEARCH_PAPER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("keywords", "VARCHAR"),
    ("grade", "VARCHAR"),
    ("grade_score", "INTEGER"),
    ("rubric_evaluation", "JSON"),
)


def _migrate_research_paper_columns() -> None:
    """Additive, idempotent migration: adds whichever of `keywords`,
    `grade`, `grade_score`, `rubric_evaluation` are missing from an
    existing `research_papers` table — same reasoning and pattern as
    _migrate_ingredient_grading_columns() above (create_all() never
    alters an existing table's columns, only creates missing tables by
    name). Safe to run on every startup; a no-op once every column
    exists, including on a freshly-created database where create_all()
    already included all of them.
    """
    if "research_papers" not in SQLModel.metadata.tables:
        return  # shouldn't happen — defensive, in case of a future rename

    with engine.connect() as connection:
        existing_columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(research_papers)")
        }
        if not existing_columns:
            return  # table doesn't exist yet somehow — create_all() above should prevent this

        for column_name, column_ddl in _RESEARCH_PAPER_COLUMNS:
            if column_name in existing_columns:
                continue
            connection.exec_driver_sql(
                f"ALTER TABLE research_papers ADD COLUMN {column_name} {column_ddl}"
            )
        connection.commit()


def init_db() -> None:
    """Creates backend/data/ (if missing) and all registered tables.

    Call once at application startup (see app/main.py's lifespan handler).
    Safe to call repeatedly — `create_all` only creates tables that don't
    already exist, and the migration steps below only add columns that
    are actually missing. Non-destructive: existing data is left alone.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine)
    _migrate_ingredient_grading_columns()
    _migrate_research_paper_columns()


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
