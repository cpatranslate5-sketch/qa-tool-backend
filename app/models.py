import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Manager(Base):
    """A "folder" in the user's terms. The very first manager ever created
    is automatically the admin — only the admin folder may create projects,
    language folders, or edit a project's glossary (see main.py's
    _require_admin). Every other folder can use whatever the admin has
    already set up (single checks, multi-uploads) but not restructure it."""

    __tablename__ = "managers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    code_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Project(Base):
    """Shared/global — every folder sees the same set of projects. Only the
    admin folder can create one (see _require_admin in main.py). No more
    per-language sub-folders (removed — see the dropped ProjectLanguage
    model): a project instead carries three optional reference documents
    (glossary, numerals/number-format, tone-of-address), each gating its
    matching AI check until uploaded — see app.main's _require_doc."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    created_by_name: Mapped[str] = mapped_column(String(120), default="")
    glossary_filename: Mapped[str] = mapped_column(String(300), default="")
    glossary_uploaded_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    numerals_filename: Mapped[str] = mapped_column(String(300), default="")
    numerals_uploaded_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tone_filename: Mapped[str] = mapped_column(String(300), default="")
    tone_uploaded_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    single_checks: Mapped[list["SingleCheck"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    multi_checks: Mapped[list["MultiCheck"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    glossary_terms: Mapped[list["GlossaryTerm"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    numeral_rules: Mapped[list["NumeralRule"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    tone_rules: Mapped[list["ToneRule"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class GlossaryTerm(Base):
    """One row of the project's master glossary doc: an EN term, an optional
    free-text description/context, and a translation per target language
    (keyed by lang_code, e.g. {"ru": "...", "es-mx": "...", ...}). Re-uploading
    the glossary file replaces all of a project's rows. A check for a given
    language only ever needs EN + RU + that one target column — see
    app.glossary.terms_for_language."""

    __tablename__ = "glossary_terms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    term_en: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    translations: Mapped[dict] = mapped_column(JSON, default=dict)
    row_order: Mapped[int] = mapped_column(Integer, default=0)

    project: Mapped["Project"] = relationship(back_populates="glossary_terms")


class NumeralRule(Base):
    """One row of the project's "Нумералс" doc: for a given language, the
    full set of number/currency/date/etc. format columns (see `fields`
    below). Row-based (one row per language), unlike the glossary's
    per-language columns."""

    __tablename__ = "numeral_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    lang_code: Mapped[str] = mapped_column(String(20), nullable=False)
    # Every number/currency/date/etc. format column found for this language
    # in the uploaded document, keyed by its (Russian) column label — e.g.
    # {"валюта при числах от 10 000": "11 500 €", "формат даты": "16.08.2023"}.
    # The real document has around a dozen such columns per language rather
    # than one free-text rule, and the agency may add more over time, so
    # every column present is kept rather than assuming a fixed set.
    fields: Mapped[dict] = mapped_column(JSON, default=dict)

    project: Mapped["Project"] = relationship(back_populates="numeral_rules")

    __table_args__ = (UniqueConstraint("project_id", "lang_code", name="uq_numeral_rule_per_project_lang"),)


class ToneRule(Base):
    """One row of the project's "Тон обращения" doc: for a given language,
    whether the required register is formal or informal. Row-based, same
    simple list style as NumeralRule."""

    __tablename__ = "tone_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    lang_code: Mapped[str] = mapped_column(String(20), nullable=False)
    # "formal" or "informal" — parsed from the doc's Russian wording.
    register: Mapped[str] = mapped_column(String(20), default="")

    project: Mapped["Project"] = relationship(back_populates="tone_rules")

    __table_args__ = (UniqueConstraint("project_id", "lang_code", name="uq_tone_rule_per_project_lang"),)


class SingleCheck(Base):
    """One source/translation pair, checked directly against a project (no
    more per-language sub-folder — source_lang/target_lang are recorded on
    the check itself, chosen at run time)."""

    __tablename__ = "single_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    # Which folder ran this check — history is now scoped per-folder (each
    # manager only ever sees their own runs), not shared across the whole
    # project like it used to be. Nullable only because rows created before
    # this column existed have no value to backfill.
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("managers.id"), nullable=True)
    source_lang: Mapped[str] = mapped_column(String(20), default="")
    target_lang: Mapped[str] = mapped_column(String(20), default="")
    source: Mapped[str] = mapped_column(Text, nullable=False)
    translation: Mapped[str] = mapped_column(Text, nullable=False)
    checks_run: Mapped[list] = mapped_column(JSON, default=list)
    findings: Mapped[list] = mapped_column(JSON, default=list)
    performed_by_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    project: Mapped["Project"] = relationship(back_populates="single_checks")


class MultiCheck(Base):
    """One multi-language Excel upload, checked in a project's "Мульти" section.

    Small/medium uploads run synchronously (status="completed" right away).
    Large ones (see excel_multi.BATCH_THRESHOLD_CHARS) are submitted through
    Anthropic's Message Batches API instead — half the per-token price, but
    not instant — and start out as status="processing", with `results`
    holding the not-yet-AI-checked skeleton (see excel_multi.build_batch_plan)
    and `batch_id` the Anthropic batch to poll. app.main's detail endpoint
    checks the batch and flips the record to "completed" once it's ended."""

    __tablename__ = "multi_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    # Same per-folder history scoping as SingleCheck.manager_id, above.
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("managers.id"), nullable=True)
    filename: Mapped[str] = mapped_column(String(300), default="")
    source_lang: Mapped[str] = mapped_column(String(20), default="en")
    checks_run: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    results: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="completed")
    batch_id: Mapped[str] = mapped_column(String(200), default="")
    performed_by_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    project: Mapped["Project"] = relationship(back_populates="multi_checks")
