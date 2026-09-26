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
    # True while the background Sonnet+GPT second-opinion pass (see
    # app.main._run_second_opinion_background) hasn't finished yet for this
    # check — false for an older record from before this field existed, same
    # as a missing key in its stored results dict. The frontend polls while
    # this is true so "Отфильтровать отчёт" waits for real percents instead
    # of quietly keeping everything (its safe fallback for a missing one).
    second_opinion_pending: bool = False

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
    # "bare" — set true to bypass our whole normal prompt in favor of a
    # minimal, direct question (no calibration wording, no JSON schema).
    bare: bool = False
    # See app.model_comparison.run_model_comparison's own docstring on
    # "two_step" — set true to run the two-step search-then-check pipeline
    # instead of the plain structured prompt. Ignored when bare=True.
    two_step: bool = False
    # Which models to compare, by name ("opus"/"sonnet"/"haiku") — see
    # app.model_comparison.DEFAULT_COMPARISON_MODELS. Defaults to sonnet +
    # haiku only (Александр's ask, 2026-09-23 — no Opus spend for a routine
    # comparison); pass ["opus", "sonnet", "haiku"] to include Opus too.
    models: list[str] = ["sonnet", "haiku"]


class ChunkTestRow(BaseModel):
    """One row for the /debug/chunk-size-comparison test — see
    app.model_comparison.run_chunk_size_comparison's own docstring."""
    context: str = ""
    source: str
    translation: str
    # Set true on any row you already know/suspect carries a real problem
    # (like the Marathi "отыгрыш" row below) — the report's headline hit-rate
    # is only averaged over rows flagged this way, since there's no way to
    # tell a genuine "nothing wrong here" from a silent miss on a row with
    # no known issue.
    has_known_issue: bool = False


class ChunkSizeComparisonIn(BaseModel):
    """Input for the standalone /debug/chunk-size-comparison endpoint — see
    app.model_comparison.run_chunk_size_comparison's own comment for what
    this tests. Unlike /debug/model-comparison (which compares MODEL
    choice on one fixed row), this compares CHUNK SIZE — checking each row
    of the same set alone (today's real behavior for HARD_LANGUAGE_BASES
    languages, one AI call per row) versus checking the whole set together
    in one prompt (today's real behavior for every other language, up to
    15 rows per call) — the actual lever behind the cost premium
    Александр asked about (2026-09-26).

    Row 0's default is the real Marathi "отыгрыш" pair (2026-09-22) — the
    ONE documented real case of this exact effect (missed in a batch,
    caught alone) — marked has_known_issue=True. The remaining default
    rows are synthetic filler (has_known_issue=False), only there to give
    the "batched" mode something to actually batch against; they don't
    prove anything on their own. For a result actually worth trusting,
    Александр should replace some or all of these via "Try it out" with
    his OWN real rows (any hard language, ideally ones he already suspects
    are borderline) before drawing conclusions."""
    rows: list[ChunkTestRow] = [
        ChunkTestRow(
            context="freebet", source="Фрибет без отыгрыша", translation="पैज न लावता फ्री बेट", has_known_issue=True,
        ),
        ChunkTestRow(context="greeting", source="Добро пожаловать!", translation="स्वागत आहे!"),
        ChunkTestRow(context="balance", source="Ваш баланс обновлён.", translation="तुमची शिल्लक अद्ययावत झाली आहे."),
        ChunkTestRow(
            context="support", source="Если появятся вопросы, напишите в поддержку.",
            translation="काही प्रश्न असल्यास, आधार सेवेशी संपर्क साधा.",
        ),
        ChunkTestRow(
            context="promo", source="Акция доступна зарегистрированным пользователям.",
            translation="ही ऑफर नोंदणीकृत वापरकर्त्यांसाठी उपलब्ध आहे.",
        ),
        ChunkTestRow(
            context="terms", source="Организатор акции — администрация сайта.",
            translation="ऑफरचे आयोजक साइट प्रशासन आहे.",
        ),
        ChunkTestRow(
            context="bonus", source="Бонус будет зачислен в течение 24 часов.",
            translation="बोनस 24 तासांच्या आत जमा केला जाईल.",
        ),
        ChunkTestRow(
            context="withdrawal", source="Вывод средств занимает до 3 рабочих дней.",
            translation="पैसे काढण्यास 3 कामकाजी दिवसांपर्यंत वेळ लागतो.",
        ),
    ]
    target_lang: str = "mr"
    source_lang: str = "ru"
    checks: list[str] = ["typo"]
    # Repetitions per mode (not per row) — "individual" mode still makes
    # one call PER ROW per repetition, so this costs more than the same
    # number in ModelComparisonIn.runs_per_model. Capped server-side at
    # app.model_comparison.MAX_RUNS_PER_CHUNK_MODE.
    runs_per_mode: int = 3
    # Defaults to whatever production actually uses today (settings.CLAUDE_MODEL)
    # when left unset — this test is about chunk size, not model choice.
    model: str | None = None
