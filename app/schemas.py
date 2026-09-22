from pydantic import BaseModel

# Every check type a single-check or multi-check can run. "missing" isn't
# here — it always runs automatically whenever a translation is empty.
# "register" (tone of address) no longer needs any project document — it
# reports the register actually used instead of judging it against one
# (see app.claude_client.summarize_register_values).
DEFAULT_CHECKS = [
    "numbers", "placeholders", "register", "typo",
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


class ManagerAdminEnterIn(BaseModel):
    # Lets someone who already has admin access on this device open any
    # other folder without typing that folder's own password — see
    # app.main.admin_enter. Only the claimed admin id is checked (it must
    # really be an admin); no password is re-verified here, matching the
    # rest of this app's device-side trust model (a folder, once unlocked
    # on a device, is simply remembered there — see FolderPicker.tsx).
    admin_manager_id: int


class ProjectIn(BaseModel):
    name: str
    manager_id: int
    # Optional — deep-copies another project's language catalog into the
    # new one as a starting point (fully independent afterward).
    copy_from_project_id: int | None = None


class ProjectDeleteIn(BaseModel):
    manager_id: int
    code: str


class ProjectOut(BaseModel):
    id: int
    name: str
    created_by_name: str

    class Config:
        from_attributes = True


class LanguageIn(BaseModel):
    manager_id: int
    lang_code: str


class LanguageAliasIn(BaseModel):
    manager_id: int
    alias: str
    canonical_code: str


class CheckIn(BaseModel):
    source: str
    translation: str
    checks: list[str] = DEFAULT_CHECKS
    # Optional — when provided, the check is saved into the requesting
    # folder's own history (see manager_id below) and the project's
    # tone-of-address document (narrowed to this one target language) is
    # used automatically.
    project_id: int | None = None
    source_lang: str = ""
    target_lang: str = ""
    # One-off instruction for this specific task only (e.g. "in this task,
    # 'Golden Spin' should be translated, not left as-is") — never saved to
    # the project.
    extra_instructions: str = ""
    # Who's running it — manager_name is just for display attribution,
    # manager_id is what scopes this check into that folder's own history
    # (see app.main.single_check_history: history is per-folder now, not
    # shared across every folder that touches a project).
    manager_name: str = ""
    manager_id: int | None = None


class Finding(BaseModel):
    type: str
    severity: str
    message: str


class CheckOut(BaseModel):
    findings: list[dict]
    single_check_id: int | None = None
    # Actual Anthropic API cost of this check's AI calls, in USD (0 when it
    # only used free rule-based criteria, or no API key is configured).
    cost_usd: float = 0.0


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
    cost_usd: float = 0.0

    class Config:
        from_attributes = True


class MultiCheckHistoryOut(BaseModel):
    id: int
    filename: str
    source_lang: str
    summary: dict
    cost_usd: float = 0.0
    status: str
    performed_by_name: str
    created_at: str
    # Only set while status is "processing" — Anthropic's own count of how
    # many of the batch's requests are done vs. the total (see
    # excel_multi._batch_progress), so the history list can show a real
    # percentage instead of just "ещё обрабатывается…".
    progress: dict | None = None
    # Only meaningful while status is "processing" — a rough, non-binding
    # ETA in minutes, learned from how long similarly-sized past batch jobs
    # actually took (see app.main._estimate_batch_minutes). None until
    # there's history to learn from, or for a check that never queued.
    estimated_minutes: int | None = None
    # Only set once status is "completed" — lets the history list show how
    # long the check actually took (created_at to completed_at), without a
    # second request. None for a still-processing check, or for an older
    # record from before this field existed.
    completed_at: str | None = None

    class Config:
        from_attributes = True


class ModelComparisonIn(BaseModel):
    """Input for the standalone /debug/model-comparison endpoint — see
    app.model_comparison's own comment for why this exists. Defaults are
    the real Marathi "отыгрыш" row (context "freebet") that started this
    whole investigation, so the interactive /docs page already has a
    meaningful example filled in without Александр needing to type
    anything — he can just press "Try it out" → "Execute" to get a first
    real result, then edit the fields to try other rows."""
    context: str = "freebet"
    source: str = "Фрибет без отыгрыша"
    translation: str = "पैज न लावता फ्री बेट"
    target_lang: str = "mr"
    source_lang: str = "ru"
    checks: list[str] = ["typo"]
    runs_per_model: int = 5
    # See app.model_comparison.run_model_comparison's own docstring on
    # "relaxed" — set true to test with the loosened confidence bar
    # instead of the normal strict one.
    relaxed: bool = False
    # See app.model_comparison.run_model_comparison's own docstring on
    # "bare" — set true to bypass our whole normal prompt in favor of a
    # minimal, direct question (no calibration wording, no JSON schema).
    # When true, "relaxed" is ignored.
    bare: bool = False
