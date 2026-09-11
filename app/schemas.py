from pydantic import BaseModel

DEFAULT_CHECKS = ["numbers", "placeholders", "glossary", "register", "typo"]


class LoginIn(BaseModel):
    name: str
    code: str


class LoginOut(BaseModel):
    manager_id: int
    name: str
    is_new: bool


class ProjectIn(BaseModel):
    name: str


class ProjectOut(BaseModel):
    id: int
    name: str
    glossary: str

    class Config:
        from_attributes = True


class GlossaryIn(BaseModel):
    glossary: str


class LanguageIn(BaseModel):
    lang_code: str


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
    # language-folder history and the project glossary is used automatically.
    project_id: int | None = None
    language_id: int | None = None
    # Only used when project_id/language_id are not given (standalone check).
    glossary: str = ""


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
    created_at: str

    class Config:
        from_attributes = True


class MultiCheckHistoryOut(BaseModel):
    id: int
    filename: str
    source_lang: str
    summary: dict
    created_at: str

    class Config:
        from_attributes = True
