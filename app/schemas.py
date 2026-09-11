import datetime

from pydantic import BaseModel

# Every check type a single-check or multi-check can run. "missing" isn't
# here — it always runs automatically whenever a translation is empty.
DEFAULT_CHECKS = [
    "numbers", "placeholders", "glossary", "register", "typo",
    "untranslatable", "completeness", "punctuation",
]


class ManagerOut(BaseModel):
    id: int
    name: str
    is_admin: bool

    class Config:
        from_attributes = True


class ManagerCreateIn(BaseModel):
    name: str
    code: str


class ManagerUnlockIn(BaseModel):
    code: str


class ProjectIn(BaseModel):
    name: str
    manager_id: int


class ProjectOut(BaseModel):
    id: int
    name: str
    glossary_filename: str
    glossary_uploaded_at: datetime.datetime | None
    created_by_name: str

    class Config:
        from_attributes = True


class GlossaryStatusOut(BaseModel):
    """The glossary is now a structured document uploaded as a file (admin
    only) — this is what every folder sees to know what's loaded, without
    exposing the full table."""

    filename: str
    uploaded_at: datetime.datetime | None
    term_count: int


class LanguageIn(BaseModel):
    lang_code: str
    manager_id: int


class LanguageOut(BaseModel):
    id: int
    lang_code: str

    class Config:
        from_attributes = True


class CheckIn(BaseModel):
    source: str
    translation: str
    checks: list[str] = DEFAULT_CHECKS
    # Optional — when provided, the check is saved into that project's
    # language-folder history and the project's glossary (narrowed to
    # EN + RU + this language) is used automatically.
    project_id: int | None = None
    language_id: int | None = None
    # Only used when project_id/language_id are not given (standalone check).
    glossary: str = ""
    # One-off instruction for this specific task only (e.g. "in this task,
    # 'Golden Spin' should be translated, not left as-is") — never saved to
    # the project, unlike the glossary.
    extra_instructions: str = ""
    # Who's running it, for shared-history attribution (any folder may check).
    manager_name: str = ""


class Finding(BaseModel):
    type: str
    severity: str
    message: str


class CheckOut(BaseModel):
    findings: list[dict]
    single_check_id: int | None = None


class SingleCheckHistoryOut(BaseModel):
    id: int
    source: str
    translation: str
    checks_run: list
    findings: list
    performed_by_name: str
    created_at: str

    class Config:
        from_attributes = True


class MultiCheckHistoryOut(BaseModel):
    id: int
    filename: str
    source_lang: str
    summary: dict
    performed_by_name: str
    created_at: str

    class Config:
        from_attributes = True
