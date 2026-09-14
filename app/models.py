import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
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
    language folders, or edit a project's reference documents (see main.py's
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
    model): a project carries one optional reference document
    (tone-of-address), gating its matching AI check until uploaded — see
    app.main's _require_doc.

    Two other documents existed here too but were removed:

    - Numerals (number/currency/date format per language) — the AI check
      built on it kept misreading the document (currency identity vs.
      format, date examples, cross-referencing unrelated fields) and
      wasn't worth the reliability cost.
    - Glossary (term-by-term required translations) — unlike Numerals,
      this one didn't need AI judgment at all (a term either matches the
      glossary or it doesn't, a plain text comparison), so letting a
      probabilistic model decide it was never the right tool to begin
      with, and it kept missing/mislabeling things as a result.

    Both were dropped rather than patched further — see git history for
    the removal commits."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    created_by_name: Mapped[str] = mapped_column(String(120), default="")
    tone_filename: Mapped[str] = mapped_column(String(300), default="")
    tone_uploaded_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    single_checks: Mapped[list["SingleCheck"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    multi_checks: Mapped[list["MultiCheck"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    tone_rules: Mapped[list["ToneRule"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class ToneRule(Base):
    """One row of the project's "Тон обращения" doc: for a given language,
    whether the required register is formal or informal."""

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
    # Actual Anthropic API cost of this check's AI calls, in USD — 0 for a
    # check that used only free rule-based criteria (punctuation, numbers,
    # placeholders) or ran with no API key configured.
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
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
    # Same as SingleCheck.cost_usd, above — summed across every language's
    # AI call for this upload. Filled in once (for a synchronous run) or
    # once the Message Batches job finalizes (for a "processing" one).
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # How many characters this upload's batch run covers (see
    # excel_multi.estimate_check_volume) — only set for a batch-path
    # submission (0 for a synchronous one, which never queued at all and so
    # has no bearing on how long the queue takes). Kept so a future batch
    # job's expected wait can be estimated from how long past jobs of a
    # similar size actually took — see app.main._estimate_batch_minutes.
    batch_volume_chars: Mapped[int] = mapped_column(Integer, default=0)
    # When this record's batch actually finished (flipped to "completed") —
    # together with created_at and batch_volume_chars, this is the
    # historical data _estimate_batch_minutes learns from. NULL for a
    # synchronous check or one still processing.
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped["Project"] = relationship(back_populates="multi_checks")
