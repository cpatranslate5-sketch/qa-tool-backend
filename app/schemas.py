import datetime

from pydantic import BaseModel

# Every check type a single-check or multi-check can run. "missing" isn't
# here — it always runs automatically whenever a translation is empty.
# "numerals" and "register" (tone of address) each require their matching
# project document to be uploaded at all — see app.main._require_doc.
DEFAULT_CHECKS = [
    "numbers", "placeholders", "glossary", "numerals", "register", "typo",
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


class ManagerChangePasswordIn(BaseModel):
    current_code: str
    new_code: str


class ProjectIn(BaseModel):
    name: str
    manager_id: int
    # Optional — deep-copies another project's glossary/numerals/tone rows
    # into the new one as a starting point (fully independent afterward).
    copy_from_project_id: int | None = None


class ProjectDeleteIn(BaseModel):
    manager_id: int
    code: str


class ProjectOut(BaseModel):
    id: int
    name: str
    glossary_filename: str
    glossary_uploaded_at: datetime.datetime | None
    numerals_filename: str
    numerals_uploaded_at: datetime.datetime | None
    tone_filename: str
    tone_uploaded_at: datetime.datetime | None
    created_by_name: str

    class Config:
        from_attributes = True


class GlossaryStatusOut(BaseModel):
    """Every one of the three project documents (glossary, numerals,
    tone-of-address) is a structured file uploaded by the admin — this is
    what every folder sees to know what's loaded, without exposing the
    full table."""

    filename: str
    uploaded_at: datetime.datetime | None
    term_count: int


class NumeralsStatusOut(BaseModel):
    filename: str
    uploaded_at: datetime.datetime | None
    rule_count: int


class ToneStatusOut(BaseModel):
    filename: str
    uploaded_at: datetime.datetime | None
    rule_count: int


class CheckIn(BaseModel):
    source: str
    translation: str
    checks: list[str] = DEFAULT_CHECKS
    # Optional — when provided, the check is saved into the project's
    # shared history and the project's glossary/numerals/tone documents
    # (narrowed to EN + RU + this one target language) are used automatically.
    project_id: int | None = None
    source_lang: str = ""
    target_lang: str = ""
    # Only used when project_id is not given (standalone check).
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
    source_lang: str
    target_lang: str
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
    status: str
    performed_by_name: str
    created_at: str

    class Config:
        from_attributes = True
