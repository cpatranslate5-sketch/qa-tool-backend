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
            # Two more optional reference documents alongside the glossary —
            # numerals (number/currency format) and tone-of-address.
            if "numerals_filename" not in cols:
                conn.execute(text("ALTER TABLE projects ADD COLUMN numerals_filename VARCHAR(300) NOT NULL DEFAULT ''"))
            if "numerals_uploaded_at" not in cols:
                conn.execute(text("ALTER TABLE projects ADD COLUMN numerals_uploaded_at TIMESTAMPTZ"))
            if "tone_filename" not in cols:
                conn.execute(text("ALTER TABLE projects ADD COLUMN tone_filename VARCHAR(300) NOT NULL DEFAULT ''"))
            if "tone_uploaded_at" not in cols:
                conn.execute(text("ALTER TABLE projects ADD COLUMN tone_uploaded_at TIMESTAMPTZ"))

    # Very old shape only (projects used to be owned by one manager) — if
    # this ever fires, there's genuinely nothing compatible to preserve, so
    # the whole cluster is rebuilt fresh. On an already-migrated database
    # (which by now includes any project the user has actually set up,
    # with real uploaded glossary data) this is always False and nothing
    # here is touched.
    insp = inspect(engine)  # re-inspect: the block above may have altered "projects"
    existing_tables = set(insp.get_table_names())
    if "projects" in existing_tables:
        cols = {c["name"] for c in insp.get_columns("projects")}
        needs_full_reset = "manager_id" in cols or "name" not in cols
        if needs_full_reset:
            with engine.begin() as conn:
                for table in ("multi_checks", "single_checks", "project_languages", "projects"):
                    conn.execute(text(f"DROP TABLE IF EXISTS {table}{cascade}"))

    # Language folders are being removed — single_checks now records
    # source_lang/target_lang directly instead of a language_id FK into the
    # (also removed) project_languages table. Reset ONLY single_checks (a
    # log of individual segment checks, not project setup — safe to lose)
    # rather than projects/multi_checks, which hold real project
    # configuration, uploaded glossary/numerals/tone documents, and
    # multi-check history/reports that must survive this upgrade.
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    if "single_checks" in existing_tables:
        sc_cols = {c["name"] for c in insp.get_columns("single_checks")}
        if "performed_by_name" not in sc_cols or "language_id" in sc_cols:
            with engine.begin() as conn:
                conn.execute(text(f"DROP TABLE IF EXISTS single_checks{cascade}"))

    # project_languages (language folders) is removed entirely — no model
    # references it anymore, and it holds nothing worth keeping (just a
    # list of lang codes, easily re-derived from the new reference docs).
    existing_tables = set(insp.get_table_names())
    if "project_languages" in existing_tables:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS project_languages{cascade}"))

    # The Numerals document turned out to have ~a dozen distinct format
    # columns per language (currency, decimal separator, date, percent,
    # number grouping, ...) rather than one free-text rule — numeral_rules
    # moves from a single rule_text column to a JSON "fields" dict holding
    # all of them. This table has no real historical data worth preserving
    # (nothing has been uploaded through it in production yet), so it's
    # safe to just add the new column and drop the old one outright.
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    if "numeral_rules" in existing_tables:
        cols = {c["name"] for c in insp.get_columns("numeral_rules")}
        with engine.begin() as conn:
            if "fields" not in cols:
                json_type = "JSONB" if engine.dialect.name == "postgresql" else "JSON"
                conn.execute(text(f"ALTER TABLE numeral_rules ADD COLUMN fields {json_type}"))
            if "rule_text" in cols:
                conn.execute(text("ALTER TABLE numeral_rules DROP COLUMN rule_text"))

    # Large multi-checks now go through Anthropic's (cheaper, slower) Message
    # Batches API instead of running live — add the columns that track that
    # without touching any existing multi_checks rows (they're simply
    # already-completed, non-batched checks).
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    if "multi_checks" in existing_tables:
        cols = {c["name"] for c in insp.get_columns("multi_checks")}
        with engine.begin() as conn:
            if "status" not in cols:
                conn.execute(text("ALTER TABLE multi_checks ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'completed'"))
            if "batch_id" not in cols:
                conn.execute(text("ALTER TABLE multi_checks ADD COLUMN batch_id VARCHAR(200) NOT NULL DEFAULT ''"))

    # History is now scoped per-folder (each manager only sees their own
    # check/upload history) instead of shared across the whole project —
    # add the column that records who ran each one. Existing rows simply
    # have no manager_id (NULL) and so won't show up in anyone's scoped
    # history anymore, which is fine — there's no real production history
    # riding on this yet.
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    if "single_checks" in existing_tables:
        cols = {c["name"] for c in insp.get_columns("single_checks")}
        if "manager_id" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE single_checks ADD COLUMN manager_id INTEGER"))
    if "multi_checks" in existing_tables:
        cols = {c["name"] for c in insp.get_columns("multi_checks")}
        if "manager_id" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE multi_checks ADD COLUMN manager_id INTEGER"))

    # Actual Anthropic API cost per check, in USD — see
    # claude_client._usage_cost. Existing rows get 0.0 (their real cost was
    # never tracked), which reads the same as "no AI check ran".
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    if "single_checks" in existing_tables:
        cols = {c["name"] for c in insp.get_columns("single_checks")}
        if "cost_usd" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE single_checks ADD COLUMN cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0"))
    if "multi_checks" in existing_tables:
        cols = {c["name"] for c in insp.get_columns("multi_checks")}
        if "cost_usd" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE multi_checks ADD COLUMN cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0"))


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
