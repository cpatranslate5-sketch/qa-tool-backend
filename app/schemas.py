from pydantic import BaseModel

DEFAULT_CHECKS = ["numbers", "placeholders", "glossary", "register", "typo"]


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
    glossary: str
    created_by_name: str

    class Config:
        from_attributes = True


class GlossaryIn(BaseModel):
    glossary: str
    manager_id: int


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
    # language-folder history and the project glossary is used automatically.
    project_id: int | None = None
    language_id: int | None = None
    # Only used when project_id/language_id are not given (standalone check).
    glossary: str = ""
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
