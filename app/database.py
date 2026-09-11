"""
DB connection setup.

On Railway, a Postgres DATABASE_URL is provided as an env var once a
Postgres service is attached to this project. Locally (or if no DB is
attached yet), we fall back to a SQLite file so the app still runs.
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

RAW_DATABASE_URL = os.environ.get("DATABASE_URL", "")

if RAW_DATABASE_URL:
    # Railway/Heroku-style URLs sometimes start with postgres:// — SQLAlchemy
    # needs the postgresql:// scheme, and we use the psycopg (v3) driver.
    url = RAW_DATABASE_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    DATABASE_URL = url
    connect_args = {}
else:
    DATABASE_URL = "sqlite:///./qa_tool.db"
    connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _run_migrations():
    """
    Small hand-rolled, idempotent migrations — this project has no Alembic
    set up, and the data so far is trivial, so we adjust the live schema
    directly instead. Safe to run on every startup.
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    cascade = " CASCADE" if engine.dialect.name == "postgresql" else ""

    if "managers" in existing_tables:
        cols = {c["name"] for c in insp.get_columns("managers")}
        if "is_admin" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE managers ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT FALSE"))

    if "projects" in existing_tables:
        cols = {c["name"] for c in insp.get_columns("projects")}
        with engine.begin() as conn:
            # The free-text glossary field was replaced by the structured
            # glossary_terms table (a file upload, not a textbox) — drop it
            # rather than leave a stale NOT NULL column that would break
            # every future insert (the ORM no longer sets it).
            if "glossary" in cols:
                conn.execute(text("ALTER TABLE projects DROP COLUMN glossary"))
            if "glossary_filename" not in cols:
                conn.execute(text("ALTER TABLE projects ADD COLUMN glossary_filename VARCHAR(300) NOT NULL DEFAULT ''"))
            if "glossary_uploaded_at" not in cols:
                conn.execute(text("ALTER TABLE projects ADD COLUMN glossary_uploaded_at TIMESTAMPTZ"))

    # The project/language/check tables changed shape (projects used to be
    # owned by one manager; now they're shared, and single_checks/multi_checks
    # gained a performed_by_name column). Rather than hand-write an ALTER for
    # every case, just drop and let create_all() below rebuild them fresh —
    # there's no meaningful data yet to preserve there, and managers (the
    # actual folders/logins) are left untouched.
    insp = inspect(engine)  # re-inspect: the block above may have altered "projects"
    existing_tables = set(insp.get_table_names())
    if "projects" in existing_tables:
        cols = {c["name"] for c in insp.get_columns("projects")}
        needs_reset = "manager_id" in cols or "name" not in cols
        if not needs_reset and "single_checks" in existing_tables:
            sc_cols = {c["name"] for c in insp.get_columns("single_checks")}
            needs_reset = "performed_by_name" not in sc_cols
        if needs_reset:
            with engine.begin() as conn:
                for table in ("multi_checks", "single_checks", "project_languages", "projects"):
                    conn.execute(text(f"DROP TABLE IF EXISTS {table}{cascade}"))


def _ensure_admin_exists():
    from app import models

    db = SessionLocal()
    try:
        has_admin = db.query(models.Manager).filter(models.Manager.is_admin.is_(True)).first()
        if has_admin:
            return
        earliest = db.query(models.Manager).order_by(models.Manager.id.asc()).first()
        if earliest:
            earliest.is_admin = True
            db.commit()
    finally:
        db.close()


def init_db():
    # Imported here to avoid circular imports at module load time.
    from app import models  # noqa: F401

    _run_migrations()
    Base.metadata.create_all(bind=engine)
    _ensure_admin_exists()
