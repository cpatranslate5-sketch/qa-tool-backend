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
    model) and, as of 2026-09-16, no more reference documents at all.

    Three documents used to live here, all removed in the end:

    - Numerals (number/currency/date format per language) — the AI check
      built on it kept misreading the document (currency identity vs.
      format, date examples, cross-referencing unrelated fields) and
      wasn't worth the reliability cost.
    - Glossary (term-by-term required translations) — unlike Numerals,
      this one didn't need AI judgment at all (a term either matches the
      glossary or it doesn't, a plain text comparison), so letting a
      probabilistic model decide it was never the right tool to begin
      with, and it kept missing/mislabeling things as a result.
    - Тон обращения (formal/informal register per language) — the register
      check used to require this doc and flag a violation against it. But
      a document that never covered every language, and a fallback for
      the languages it didn't, meant the check could be structurally
      blind to a translator using the SAME wrong register in every single
      row (see app.claude_client's register_value machinery for the full
      story) — Александр asked to drop the whole document and replace the
      check with a plain factual report instead ("Тон обращения: везде на
      «вы»", "...кроме: строки 5, 12"): no more pass/fail judgment call
      for the AI to get wrong, just what register was actually used,
      which the manager reads and judges for themselves.

    All three were dropped rather than patched further — see git history
    for the removal commits."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    created_by_name: Mapped[str] = mapped_column(String(120), default="")
    # 2026-09-29 (Александр): free-text project description written by the
    # admin (subject area, audience, special requirements) — sent to the AI
    # with every check in this project, see app.main._with_domain_note.
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # 2026-10-04 (Александр): projects live inside a client («Заказчик») and
    # inherit its styleguide. `styleguide` holds ONLY this project's own
    # overrides — {lang key: {section: value}}; a section missing here means
    # «как у заказчика» (see app.styleguide.effective_rules).
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True)
    styleguide: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    client: Mapped["Client | None"] = relationship(back_populates="projects")
    single_checks: Mapped[list["SingleCheck"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    multi_checks: Mapped[list["MultiCheck"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    language_catalog: Mapped[list["LanguageCatalogEntry"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Client(Base):
    """«Заказчик» (2026-10-04, Александр): a folder of projects that share one
    styleguide — rules per language (tone of address, buttons, punctuation,
    terms…), see app.styleguide. Projects may override any section."""

    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    styleguide: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Which one-time seed steps were already applied (see main._seed_clients).
    seed_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Subject area of all its projects (2026-10-05): "betting" (betting &
    # gambling), "marketing" (general advertising) or "" (not set) — picks
    # the «ТЕМАТИКА» note every AI prompt gets, see claude_client.
    domain: Mapped[str] = mapped_column(String(20), default="", server_default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    projects: Mapped[list["Project"]] = relationship(back_populates="client")


class StyleguideChange(Base):
    """One edit of a styleguide section — the «История изменений» list, with
    «Вернуть». value_before/value_after are the section's JSON values; for a
    project, None means «как у заказчика» (no own override)."""

    __tablename__ = "styleguide_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    lang: Mapped[str] = mapped_column(String(40), default="")
    section: Mapped[str] = mapped_column(String(60), default="")
    value_before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    value_after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    changed_by_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class LearningItem(Base):
    """«Обучение платформы» inbox (2026-10-04, Александр): a finding that may
    teach the platform something — removed by the head of QA with «На
    обучение», or rejected by a translator («Не актуально», with or without a
    comment). Nothing is learned until the admin verifies it in the
    «Обучение платформы» folder (→ Lesson). status: new | postponed |
    dismissed | learned."""

    __tablename__ = "learning_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    origin: Mapped[str] = mapped_column(String(20), default="translator")  # "okk" | "translator"
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    multi_check_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    finding_key: Mapped[str] = mapped_column(String(120), nullable=False)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    project_name: Mapped[str] = mapped_column(String(200), default="")
    client_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    filename: Mapped[str] = mapped_column(String(300), default="")
    lang_code: Mapped[str] = mapped_column(String(40), default="")
    lang_key: Mapped[str] = mapped_column(String(40), default="")
    excel_row: Mapped[int] = mapped_column(Integer, default=0)
    context: Mapped[str] = mapped_column(Text, default="")
    finding_type: Mapped[str] = mapped_column(String(60), default="")
    finding_message: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(Text, default="")
    translation: Mapped[str] = mapped_column(Text, default="")
    translator_comment: Mapped[str] = mapped_column(Text, default="")
    okk_note: Mapped[str] = mapped_column(Text, default="")
    lesson_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolved_by_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("multi_check_id", "finding_key", "origin", name="uq_learning_item"),)


class Lesson(Base):
    """A verified lesson: what the checking model must take into account.
    Scope: project_id set → that project only; else client_id set → every
    project of that client; both empty → all projects. lang_key set → that
    language only; empty → all languages. status: active | disabled |
    deleted (deleted lessons stay for the history)."""

    __tablename__ = "lessons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    client_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    lang_key: Mapped[str] = mapped_column(String(40), default="", index=True)
    example: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # finding/source/translation it came from
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class LessonHistory(Base):
    """Every change of a lesson (created / edited / disabled / enabled /
    deleted / restored) with a snapshot — «История запомненного»."""

    __tablename__ = "lesson_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lesson_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    action: Mapped[str] = mapped_column(String(20), default="")
    snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    by_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class LanguageCatalogEntry(Base):
    """One language in a project's manually-curated "which languages do I
    check here" catalog — this is what the target-language checkboxes are
    built from (see app.main's /projects/{id}/known-languages and
    /projects/{id}/languages).

    Deliberately its OWN table, separate from the now-removed ToneRule:
    Александр asked for this list to change ONLY when he explicitly adds
    or removes a language — never as a side effect of uploading a
    Tone-of-address document (back when that existed — see Project's
    docstring) or a file to check (which used to get unioned into this
    same list automatically — that's exactly what let a mislabeled
    column like a stray "PR" silently show up as a real target language
    with Peru's flag). See app.database._run_migrations for the one-time
    backfill that seeded this table from each project's tone_rules the
    first time this table was created, so upgrading never blanked out
    anyone's already-built checkbox list."""

    __tablename__ = "language_catalog"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    lang_code: Mapped[str] = mapped_column(String(20), nullable=False)

    project: Mapped["Project"] = relationship(back_populates="language_catalog")

    __table_args__ = (UniqueConstraint("project_id", "lang_code", name="uq_catalog_lang_per_project"),)


class LanguageAlias(Base):
    """A GLOBAL (not per-project) dictionary of "this raw spelling means
    this language" — Александр's own idea, born from the GEO/PR-for-
    Portuguese/HING kind of mislabeling this app has been chasing case by
    case: instead of every new nonstandard abbreviation needing a code
    change from a developer, any manager teaches the platform a spelling
    once, here, and every project benefits immediately, from then on.

    Deliberately separate from LanguageCatalogEntry, which stays
    per-project and controls WHICH languages actually get checked. This
    table only affects RECOGNITION — turning a raw column header (in an
    uploaded check file, the Tone-of-address document, or even a
    manually-typed catalog addition) into the canonical code the rest of
    the app already understands, before any per-project list is even
    consulted. See app.excel_multi._label_to_code, the single choke point
    that applies this.

    `alias` is stored lower-cased and trimmed, and is globally unique —
    one spelling means exactly one language; to repoint it, delete and
    re-add rather than having two conflicting rows. `canonical_code` is
    stored already normalized (via _normalize_lang_label), so it's
    immediately usable wherever a canonical code is expected."""

    __tablename__ = "language_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alias: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    canonical_code: Mapped[str] = mapped_column(String(20), nullable=False)
    added_by_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


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
    # 2026-09-29 (Александр): the manager's review of this report before
    # sending it to translators — {finding_key: {"decision": "accept"|"question"
    # |"reject"|None, "links": "<Crowdin link(s)>", "note": "<manager's note>"}}.
    # "tone|<lang>" is the tone-of-address summary (always №1), "note|<lang>"
    # a general note for a language without one. finding_key is
    # "<sheet index>|<lang>|<excel row>|<finding index in that row>" (see the
    # frontend's filteredReport.ts reviewKey). Old reports start empty.
    review: Mapped[dict] = mapped_column(JSON, default=dict)
    # 2026-09-30 (Александр): translators' answers from their share-link page
    # — {finding_key: {"decision": "accept"|"reject"|None, "comment": str}}.
    # Kept apart from the manager's review so neither side overwrites the other.
    translator_review: Mapped[dict] = mapped_column(JSON, default=dict)
    # 2026-10-01: set when this record is the report view of a single
    # («точечная») check — it then reuses the whole review / translator-link
    # flow, and is hidden from the file-check history.
    single_check_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # When this record's batch actually finished (flipped to "completed") —
    # together with created_at and batch_volume_chars, this is the
    # historical data _estimate_batch_minutes learns from. NULL for a
    # synchronous check or one still processing.
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped["Project"] = relationship(back_populates="multi_checks")
    share_links: Mapped[list["ShareLink"]] = relationship(
        back_populates="multi_check", cascade="all, delete-orphan",
    )


class ShareLink(Base):
    """A public, read-only link to ONE language of ONE multi-check report,
    for translators (2026-09-29, Александр). The random token is the only
    key — the page it opens (app.main.shared_report_page) shows nothing but
    that language's ACCEPTED findings, rendered live from the current review,
    so later edits show up for everyone who has the link. No expiry; the
    manager can revoke it."""
    __tablename__ = "share_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    multi_check_id: Mapped[int] = mapped_column(ForeignKey("multi_checks.id"), nullable=False)
    lang: Mapped[str] = mapped_column(String(40), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    multi_check: Mapped["MultiCheck"] = relationship(back_populates="share_links")


class SavedCase(Base):
    """«Сохранённое» (2026-10-01, Александр): interesting findings a manager
    saved from a report with the 💾 button — one row of one language, with
    its source, translation and the platform's comments, copied here so it
    survives even if the report itself is deleted later. Private to the
    folder that saved it (Александр, 2026-10-01)."""
    __tablename__ = "saved_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # "<multi_check id>|<sheet index>|<lang>|<excel row>" — the same block
    # can't be saved twice.
    source_key: Mapped[str] = mapped_column(String(200), unique=True, index=True, nullable=False)
    multi_check_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The folder that saved it — only that folder sees it (2026-10-01).
    manager_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    project_name: Mapped[str] = mapped_column(String(200), default="")
    filename: Mapped[str] = mapped_column(String(300), default="")
    lang: Mapped[str] = mapped_column(String(40), default="")
    excel_row: Mapped[int] = mapped_column(Integer, default=0)
    context: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(Text, default="")
    translation: Mapped[str] = mapped_column(Text, default="")
    findings: Mapped[list] = mapped_column(JSON, default=list)
    saved_by_name: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
