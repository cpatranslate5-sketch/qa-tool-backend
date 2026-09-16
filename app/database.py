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
            # A much older free-text glossary field, from before there was
            # any structured document upload at all — long gone by now, but
            # dropped here rather than left as a stale NOT NULL column that
            # would break every future insert (the ORM no longer sets it).
            if "glossary" in cols:
                conn.execute(text("ALTER TABLE projects DROP COLUMN glossary"))
            # The structured glossary (glossary_filename/glossary_uploaded_at
            # + the glossary_terms table) was later removed entirely too —
            # see the migration further below that drops it, alongside
            # Numerals. Tone-of-address (tone_filename/tone_uploaded_at) was
            # the last reference document, dropped 2026-09-16 — see the
            # dedicated migration further below rather than an ADD-COLUMN
            # here, since Project no longer has these fields at all.

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
    # configuration, uploaded reference documents, and multi-check
    # history/reports that must survive this upgrade.
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

    # The Numerals document/check is removed entirely — the AI check built
    # on it kept misreading it in ways that weren't worth patching further
    # (currency identity vs. format, date examples turning into ISO
    # gibberish, cross-referencing unrelated fields). Drops its table and
    # the two project columns that tracked its upload; nothing else is
    # affected by THIS migration (glossary is dropped by the migration
    # further below; tone-of-address and check history aren't touched at
    # all).
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    if "numeral_rules" in existing_tables:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS numeral_rules{cascade}"))
    if "projects" in existing_tables:
        cols = {c["name"] for c in insp.get_columns("projects")}
        with engine.begin() as conn:
            if "numerals_filename" in cols:
                conn.execute(text("ALTER TABLE projects DROP COLUMN numerals_filename"))
            if "numerals_uploaded_at" in cols:
                conn.execute(text("ALTER TABLE projects DROP COLUMN numerals_uploaded_at"))

    # The Glossary document/check is removed entirely too — unlike Numerals,
    # this one never needed AI judgment at all (a term either matches the
    # glossary or it doesn't, a plain text comparison), so a probabilistic
    # model was never the right tool for it and it kept missing/mislabeling
    # things as a result. Drops its table and the two project columns that
    # tracked its upload; tone-of-address and check history are unaffected.
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    if "glossary_terms" in existing_tables:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS glossary_terms{cascade}"))
    if "projects" in existing_tables:
        cols = {c["name"] for c in insp.get_columns("projects")}
        with engine.begin() as conn:
            if "glossary_filename" in cols:
                conn.execute(text("ALTER TABLE projects DROP COLUMN glossary_filename"))
            if "glossary_uploaded_at" in cols:
                conn.execute(text("ALTER TABLE projects DROP COLUMN glossary_uploaded_at"))

    # Rough ETA for a still-processing batch job, learned from how long past
    # jobs of a similar size actually took (Александр asked for some kind of
    # estimate, even an approximate one, instead of only elapsed time — see
    # app.main._estimate_batch_minutes). batch_volume_chars records each
    # batch job's own size; completed_at records when it actually finished,
    # so the two together become the historical data future estimates learn
    # from. Existing rows get 0/NULL, i.e. "no size on record, don't count
    # this one" — harmless, since a synchronous (non-batch) check was never
    # going to be useful queue-duration data anyway.
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    if "multi_checks" in existing_tables:
        cols = {c["name"] for c in insp.get_columns("multi_checks")}
        with engine.begin() as conn:
            if "batch_volume_chars" not in cols:
                conn.execute(text("ALTER TABLE multi_checks ADD COLUMN batch_volume_chars INTEGER NOT NULL DEFAULT 0"))
            if "completed_at" not in cols:
                conn.execute(text("ALTER TABLE multi_checks ADD COLUMN completed_at TIMESTAMPTZ"))

    # The target-language checkbox catalog is now its own table, fully
    # decoupled from tone_rules — Александр asked for the checkbox list to
    # change ONLY when he explicitly adds or removes a language, never as
    # a side effect of uploading a Tone-of-address document or a file to
    # check (see models.LanguageCatalogEntry's docstring for the full
    # story — a stray mislabeled column like "PR" used to silently become
    # a real target language with Peru's flag). NOT the same table as the
    # old dropped "project_languages" (language folders, removed long
    # ago) — deliberately a different name, so the unconditional DROP
    # TABLE migration for that old name (further up) never touches this
    # one. Created and backfilled here, ONCE: the very first time this
    # table doesn't exist yet, every project's EXISTING tone_rules
    # languages are copied in, so upgrading never blanks out anyone's
    # already-built checkbox list. On every later startup the table
    # already exists and this whole block is skipped — a manager's own
    # add/remove edits (and the fact that new tone_rules no longer
    # auto-populate this table) are permanent from that point on.
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    if "language_catalog" not in existing_tables:
        with engine.begin() as conn:
            if engine.dialect.name == "postgresql":
                conn.execute(text(
                    "CREATE TABLE language_catalog ("
                    "id SERIAL PRIMARY KEY, "
                    "project_id INTEGER NOT NULL REFERENCES projects(id), "
                    "lang_code VARCHAR(20) NOT NULL, "
                    "CONSTRAINT uq_catalog_lang_per_project UNIQUE (project_id, lang_code))"
                ))
            else:
                conn.execute(text(
                    "CREATE TABLE language_catalog ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "project_id INTEGER NOT NULL REFERENCES projects(id), "
                    "lang_code VARCHAR(20) NOT NULL, "
                    "CONSTRAINT uq_catalog_lang_per_project UNIQUE (project_id, lang_code))"
                ))
            if "tone_rules" in existing_tables:
                conn.execute(text(
                    "INSERT INTO language_catalog (project_id, lang_code) "
                    "SELECT DISTINCT project_id, lang_code FROM tone_rules"
                ))

    # A GLOBAL (not per-project) dictionary of "this raw spelling means
    # this language" — Александр's own idea, so a manager can teach the
    # platform a new nonstandard abbreviation (GEO, a mistaken "PR" for
    # Portuguese, etc.) themselves, without needing a code change every
    # time. See models.LanguageAlias for the full story. Nothing to
    # backfill — this is a brand-new concept, not a replacement for
    # anything that already existed, so it always starts empty.
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    if "language_aliases" not in existing_tables:
        with engine.begin() as conn:
            if engine.dialect.name == "postgresql":
                conn.execute(text(
                    "CREATE TABLE language_aliases ("
                    "id SERIAL PRIMARY KEY, "
                    "alias VARCHAR(60) NOT NULL UNIQUE, "
                    "canonical_code VARCHAR(20) NOT NULL, "
                    "added_by_name VARCHAR(120) NOT NULL DEFAULT '', "
                    "created_at TIMESTAMPTZ)"
                ))
            else:
                conn.execute(text(
                    "CREATE TABLE language_aliases ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "alias VARCHAR(60) NOT NULL UNIQUE, "
                    "canonical_code VARCHAR(20) NOT NULL, "
                    "added_by_name VARCHAR(120) NOT NULL DEFAULT '', "
                    "created_at TIMESTAMP)"
                ))

    # Тон обращения (the register document) is removed entirely, 2026-09-16
    # — Александр asked to drop it and have the register check report the
    # actually-used tone instead of judging it against a per-language rule
    # (see models.Project's docstring, and app.claude_client's
    # register_value/summarize_register_values). The table and the two
    # project columns that tracked its upload are dropped here; nothing
    # else (the language catalog already has its own table, backfilled
    # from tone_rules long ago by the migration above — this runs after
    # that block specifically so a not-yet-migrated install still gets its
    # one-time backfill before the source table disappears).
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    if "tone_rules" in existing_tables:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS tone_rules{cascade}"))
    if "projects" in existing_tables:
        cols = {c["name"] for c in insp.get_columns("projects")}
        with engine.begin() as conn:
            if "tone_filename" in cols:
                conn.execute(text("ALTER TABLE projects DROP COLUMN tone_filename"))
            if "tone_uploaded_at" in cols:
                conn.execute(text("ALTER TABLE projects DROP COLUMN tone_uploaded_at"))


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
