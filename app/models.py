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
    admin folder can create one (see _require_admin in main.py)."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    glossary: Mapped[str] = mapped_column(Text, default="")
    created_by_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    languages: Mapped[list["ProjectLanguage"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    single_checks: Mapped[list["SingleCheck"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    multi_checks: Mapped[list["MultiCheck"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class ProjectLanguage(Base):
    __tablename__ = "project_languages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    lang_code: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    project: Mapped["Project"] = relationship(back_populates="languages")
    single_checks: Mapped[list["SingleCheck"]] = relationship(back_populates="language", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("project_id", "lang_code", name="uq_lang_per_project"),)


class SingleCheck(Base):
    """One source/translation pair checked inside a project's language folder."""

    __tablename__ = "single_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    language_id: Mapped[int] = mapped_column(ForeignKey("project_languages.id"), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    translation: Mapped[str] = mapped_column(Text, nullable=False)
    checks_run: Mapped[list] = mapped_column(JSON, default=list)
    findings: Mapped[list] = mapped_column(JSON, default=list)
    performed_by_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    project: Mapped["Project"] = relationship(back_populates="single_checks")
    language: Mapped["ProjectLanguage"] = relationship(back_populates="single_checks")


class MultiCheck(Base):
    """One multi-language Excel upload, checked in a project's "Мульти" section."""

    __tablename__ = "multi_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    filename: Mapped[str] = mapped_column(String(300), default="")
    source_lang: Mapped[str] = mapped_column(String(20), default="en")
    checks_run: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    results: Mapped[dict] = mapped_column(JSON, default=dict)
    performed_by_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    project: Mapped["Project"] = relationship(back_populates="multi_checks")
