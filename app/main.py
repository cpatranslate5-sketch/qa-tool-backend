from fastapi import Depends, FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import hash_code, verify_code
from app.claude_client import run_ai_checks
from app.config import settings
from app.database import get_db, init_db
from app.excel_multi import (
    BATCH_THRESHOLD_CHARS,
    build_batch_plan,
    build_report_workbook,
    estimate_check_volume,
    finalize_batch_results,
    merge_lang_codes,
    parse_workbook,
    pick_source_lang,
    resolve_lang_code,
    run_multi_check,
    submit_multi_check_batch,
    try_finalize_batch,
)
from app.project_docs import parse_tone_workbook
from app.rule_checks import run_rule_checks

app = FastAPI(title="Translation QA Tool", version="0.4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/health")
def health():
    return {"ok": True}


# -------------------------------------------------------------- managers ----
# A "manager" is a folder in the user's terms. The first one ever created is
# automatically the admin (see database._ensure_admin_exists) — only the
# admin folder can create/delete projects, edit a project's reference
# documents, or use the "copy requirements" template feature. Every folder
# (including the admin's) can change its own password.

@app.get("/managers", response_model=list[schemas.ManagerOut])
def list_managers(db: Session = Depends(get_db)):
    # Admin folder always first (point 2 of Александр's folder-access
    # spec) — the frontend highlights whichever entry comes first, so the
    # ordering itself is what decides that, not just a display convention.
    return db.query(models.Manager).order_by(models.Manager.is_admin.desc(), models.Manager.id.asc()).all()


@app.post("/managers", response_model=schemas.ManagerOut)
def create_manager(payload: schemas.ManagerCreateIn, db: Session = Depends(get_db)):
    name = payload.name.strip()
    code = payload.code.strip()
    if not name or not code:
        raise HTTPException(400, "Введите имя папки и код.")

    exists = db.query(models.Manager).filter(models.Manager.name == name).first()
    if exists:
        raise HTTPException(409, "Папка с таким именем уже есть.")

    is_first_ever = db.query(models.Manager).first() is None
    manager = models.Manager(name=name, code_hash=hash_code(code), is_admin=is_first_ever)
    db.add(manager)
    db.commit()
    db.refresh(manager)
    return manager


@app.post("/managers/{manager_id}/unlock", response_model=schemas.ManagerOut)
def unlock_manager(manager_id: int, payload: schemas.ManagerUnlockIn, db: Session = Depends(get_db)):
    manager = db.get(models.Manager, manager_id)
    if manager is None:
        raise HTTPException(404, "Папка не найдена.")
    if not verify_code(payload.code.strip(), manager.code_hash):
        raise HTTPException(401, "Неверный код.")
    return manager


@app.post("/managers/{manager_id}/change-password", response_model=schemas.ManagerOut)
def change_password(manager_id: int, payload: schemas.ManagerChangePasswordIn, db: Session = Depends(get_db)):
    """Available to every folder, not just the admin — each manager owns
    their own password."""
    manager = _get_manager(manager_id, db)
    if not verify_code(payload.current_code.strip(), manager.code_hash):
        raise HTTPException(401, "Текущий пароль неверен.")
    new_code = payload.new_code.strip()
    if not new_code:
        raise HTTPException(400, "Введите новый пароль.")
    manager.code_hash = hash_code(new_code)
    db.commit()
    db.refresh(manager)
    return manager


@app.post("/managers/{manager_id}/admin-enter", response_model=schemas.ManagerOut)
def admin_enter(manager_id: int, payload: schemas.ManagerAdminEnterIn, db: Session = Depends(get_db)):
    """Lets someone who already has admin access on this device open any
    other folder without typing that folder's own password (point 2 of
    Александр's spec). Trusts the client's already-established admin
    unlock the same way this app trusts its per-device remembered-folder
    cache everywhere else (there's no server-side session at all) — it
    only checks that the claimed admin id genuinely is an admin, not a
    fresh password for either folder."""
    _require_admin(payload.admin_manager_id, db)
    return _get_manager(manager_id, db)


def _get_manager(manager_id: int, db: Session) -> models.Manager:
    manager = db.get(models.Manager, manager_id)
    if manager is None:
        raise HTTPException(404, "Папка не найдена.")
    return manager


def _require_admin(manager_id: int, db: Session) -> models.Manager:
    manager = _get_manager(manager_id, db)
    if not manager.is_admin:
        raise HTTPException(403, "Это действие доступно только админской папке.")
    return manager


# -------------------------------------------------------------- projects ----
# Shared/global: every folder sees the same projects. Only the admin folder
# may create, delete, or restructure one (reference documents). No more
# per-language sub-folders — a project instead carries one optional
# reference document (tone-of-address).

@app.get("/projects", response_model=list[schemas.ProjectOut])
def list_projects(db: Session = Depends(get_db)):
    return db.query(models.Project).order_by(models.Project.name).all()


def _copy_project_documents(from_project_id: int, to_project_id: int, db: Session) -> None:
    """Deep-copies tone-of-address rows from one project into another as an
    independent starting point — editing the new project's copy afterward
    never touches the original."""
    from_project = db.get(models.Project, from_project_id)
    if from_project is None:
        raise HTTPException(404, "Проект-образец не найден.")
    to_project = db.get(models.Project, to_project_id)

    for r in db.query(models.ToneRule).filter(models.ToneRule.project_id == from_project_id).all():
        db.add(models.ToneRule(project_id=to_project_id, lang_code=r.lang_code, register=r.register))

    to_project.tone_filename = from_project.tone_filename
    to_project.tone_uploaded_at = from_project.tone_uploaded_at


@app.post("/projects", response_model=schemas.ProjectOut)
def create_project(payload: schemas.ProjectIn, db: Session = Depends(get_db)):
    manager = _require_admin(payload.manager_id, db)
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Введите название проекта.")
    exists = db.query(models.Project).filter(models.Project.name == name).first()
    if exists:
        raise HTTPException(409, "Проект с таким названием уже есть.")
    project = models.Project(name=name, created_by_name=manager.name)
    db.add(project)
    db.flush()  # assigns project.id, without committing yet

    if payload.copy_from_project_id is not None:
        _copy_project_documents(payload.copy_from_project_id, project.id, db)

    db.commit()
    db.refresh(project)
    return project


@app.delete("/projects/{project_id}")
def delete_project(project_id: int, payload: schemas.ProjectDeleteIn, db: Session = Depends(get_db)):
    manager = _require_admin(payload.manager_id, db)
    if not verify_code(payload.code.strip(), manager.code_hash):
        raise HTTPException(401, "Неверный пароль.")
    project = _get_project(project_id, db)
    db.delete(project)
    db.commit()
    return {"ok": True}


def _get_project(project_id: int, db: Session) -> models.Project:
    project = db.get(models.Project, project_id)
    if project is None:
        raise HTTPException(404, "Проект не найден.")
    return project


# ------------------------------------------------- reference documents ----
# One optional per-project document (tone-of-address), gating its matching
# AI check (see _require_doc): a check can't run at all for a project with
# zero rows in the document, but a document missing just one particular
# language only skips that language's check, rather than blocking the run.

def _tone_lookup(project_id: int, db: Session):
    """Returns callable(lang_code) -> "formal"/"informal"/"" for the closest
    matching language actually present in the project's Tone-of-address
    document.

    The document can name the same language at two granularities within
    itself (a plain "ko" row alongside a region-qualified "ko-KR" one) —
    resolve_lang_code bridges that by matching on any shared subtag, but
    only when it's unambiguous (see its docstring)."""
    rows = db.query(models.ToneRule).filter(models.ToneRule.project_id == project_id).all()
    by_lang = {r.lang_code: r.register for r in rows}

    def lookup(lang_code: str) -> str:
        resolved = resolve_lang_code(lang_code, by_lang.keys(), values=by_lang)
        return by_lang.get(resolved, "") if resolved else ""

    return lookup


# check key -> (doc name shown to the user, row-count query) — used by
# _require_doc to block a check that has nothing to check against at all.
_DOC_REQUIREMENTS = {
    "register": ("Тон обращения", models.ToneRule),
}


def _require_doc(project_id: int, checks: list[str], db: Session) -> None:
    for check_key, (doc_name, model_cls) in _DOC_REQUIREMENTS.items():
        if check_key not in checks:
            continue
        has_rows = db.query(model_cls).filter(model_cls.project_id == project_id).first() is not None
        if not has_rows:
            raise HTTPException(
                400,
                f"Для проверки «{doc_name}» нужно сначала загрузить документ «{doc_name}» для этого проекта.",
            )


@app.post("/projects/{project_id}/tone/upload", response_model=schemas.ToneStatusOut)
async def upload_tone(
    project_id: int,
    file: UploadFile = File(...),
    manager_id: int = Form(...),
    db: Session = Depends(get_db),
):
    """Admin-only. Replaces the project's whole Tone-of-address doc —
    language codes across the header row, "Формальное"/"Неформальное
    обращение" in the row(s) below each one."""
    _require_admin(manager_id, db)
    project = _get_project(project_id, db)
    file_bytes = await file.read()

    try:
        rows = parse_tone_workbook(file_bytes)
    except Exception:
        raise HTTPException(400, "Не удалось прочитать файл — убедитесь, что это .xlsx со списком языков.")
    if not rows:
        raise HTTPException(400, "В файле не найдено ни одной строки с языком и указанием тона.")

    db.query(models.ToneRule).filter(models.ToneRule.project_id == project_id).delete()
    for r in rows:
        db.add(models.ToneRule(project_id=project_id, lang_code=r["lang_code"], register=r["register"]))
    project.tone_filename = file.filename or "tone.xlsx"
    project.tone_uploaded_at = models._now()
    db.commit()

    return schemas.ToneStatusOut(
        filename=project.tone_filename, uploaded_at=project.tone_uploaded_at, rule_count=len(rows)
    )


@app.get("/projects/{project_id}/tone/status", response_model=schemas.ToneStatusOut)
def tone_status(project_id: int, db: Session = Depends(get_db)):
    project = _get_project(project_id, db)
    rule_count = db.query(models.ToneRule).filter(models.ToneRule.project_id == project_id).count()
    return schemas.ToneStatusOut(
        filename=project.tone_filename, uploaded_at=project.tone_uploaded_at, rule_count=rule_count
    )


@app.get("/projects/{project_id}/known-languages")
def known_languages(project_id: int, db: Session = Depends(get_db)):
    """Every language named in the project's tone-of-address document —
    the frontend's source for the target-language checkbox list (see
    point 8 of the redesign).

    merge_lang_codes collapses same-language entries at different
    granularities into one (keeping the more specific spelling) while
    keeping genuinely distinct regional variants (es-ES vs es-AR vs
    es-MX) separate, since those really do mean different rules and must
    be picked explicitly."""
    _get_project(project_id, db)
    langs: set[str] = set()
    for row in db.query(models.ToneRule.lang_code).filter(models.ToneRule.project_id == project_id).all():
        langs.add(row[0])
    langs.discard("")
    return {"languages": merge_lang_codes(langs)}


# --------------------------------------------------------- single check ---
# Any folder may run checks — only structural changes above are admin-only.

@app.post("/check", response_model=schemas.CheckOut)
async def check(payload: schemas.CheckIn, db: Session = Depends(get_db)):
    if not payload.source.strip() or not payload.translation.strip():
        return schemas.CheckOut(findings=[])

    tone_register = ""
    project = None
    if payload.project_id:
        project = _get_project(payload.project_id, db)
        _require_doc(payload.project_id, payload.checks, db)
        target_lang = payload.target_lang.strip().lower()
        tone_register = _tone_lookup(project.id, db)(target_lang)

    findings = run_rule_checks(
        payload.source, payload.translation, payload.checks,
        lang_code=payload.target_lang,
    )
    ai_findings, cost_usd = await run_ai_checks(
        payload.source, payload.translation, payload.checks, payload.extra_instructions,
        tone_register, payload.target_lang, payload.source_lang,
    )
    findings += ai_findings

    single_check_id = None
    if project is not None:
        record = models.SingleCheck(
            project_id=project.id,
            source_lang=payload.source_lang.strip().lower(),
            target_lang=payload.target_lang.strip().lower(),
            source=payload.source,
            translation=payload.translation,
            checks_run=payload.checks,
            findings=findings,
            performed_by_name=payload.manager_name.strip(),
            manager_id=payload.manager_id,
            cost_usd=cost_usd,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        single_check_id = record.id

    return schemas.CheckOut(findings=findings, single_check_id=single_check_id, cost_usd=cost_usd)


@app.get(
    "/projects/{project_id}/history",
    response_model=list[schemas.SingleCheckHistoryOut],
)
def single_check_history(project_id: int, manager_id: int, db: Session = Depends(get_db)):
    """Scoped to the requesting folder only — each manager sees their own
    check history, not every folder's (point 1 of Александр's spec)."""
    _get_project(project_id, db)
    records = db.query(models.SingleCheck).filter(
        models.SingleCheck.project_id == project_id,
        models.SingleCheck.manager_id == manager_id,
    ).order_by(models.SingleCheck.created_at.desc()).limit(50).all()
    return [
        schemas.SingleCheckHistoryOut(
            id=r.id, source_lang=r.source_lang, target_lang=r.target_lang,
            source=r.source, translation=r.translation,
            checks_run=r.checks_run, findings=r.findings,
            performed_by_name=r.performed_by_name,
            created_at=r.created_at.isoformat(),
            cost_usd=r.cost_usd,
        )
        for r in records
    ]


# ---------------------------------------------------------- multi check ---

DEFAULT_MULTI_CHECKS = [
    "numbers", "placeholders", "max_length", "register", "typo",
    "untranslatable", "completeness", "punctuation",
]


@app.post("/projects/{project_id}/multi-check/detect-languages")
async def detect_file_languages(project_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Language codes found as column headers in an uploaded file — lets the
    UI populate the target-language checkboxes from the file the manager is
    ABOUT to check, rather than only from the project's Tone-of-address
    document (see known_languages above). Александр hit a real gap here: a
    language can be legitimately present in a file without ever needing a
    tone-of-address rule (English chiefly, which rarely needs a ты/вы-style
    distinction) — so it simply never had a row in the Tone document, never
    appeared in known_languages, and was therefore never even offered as a
    selectable target at all, no matter what the uploaded file actually
    contained. Read-only: just parses the file and reports what's in it,
    doesn't run any check or store anything."""
    _get_project(project_id, db)
    file_bytes = await file.read()
    try:
        sheets = parse_workbook(file_bytes)
    except Exception:
        raise HTTPException(400, "Не удалось прочитать файл — убедитесь, что это .xlsx с языковыми колонками.")
    langs: set[str] = set()
    for s in sheets:
        langs.update(s["languages"])
    return {"languages": merge_lang_codes(langs)}


@app.post("/projects/{project_id}/multi-check")
async def multi_check(
    project_id: int,
    file: UploadFile = File(...),
    source_lang: str = Form(""),
    manager_name: str = Form(""),
    # Whose folder this upload belongs to — scopes it into that folder's
    # own history (see multi_check_history) rather than every folder's.
    manager_id: int = Form(...),
    extra_instructions: str = Form(""),
    # Comma-separated check keys from the UI's checkboxes; empty/absent falls
    # back to the full default set.
    checks: str = Form(""),
    # Comma-separated target language codes; empty/absent means every
    # language column found in the file (a manager can check only a
    # subset of a large upload — see point 8 of the redesign).
    target_langs: str = Form(""),
    db: Session = Depends(get_db),
):
    _get_project(project_id, db)
    _get_manager(manager_id, db)
    file_bytes = await file.read()

    try:
        sheets = parse_workbook(file_bytes)
    except Exception:
        raise HTTPException(400, "Не удалось прочитать файл — убедитесь, что это .xlsx с языковыми колонками.")

    if not sheets:
        raise HTTPException(400, "В файле не найдено ни одной колонки с кодом языка.")

    selected_checks = [c.strip() for c in checks.split(",") if c.strip()] or DEFAULT_MULTI_CHECKS
    _require_doc(project_id, selected_checks, db)

    target_filter = {c.strip().lower() for c in target_langs.split(",") if c.strip()} or None
    resolved_source = pick_source_lang(sheets, source_lang.strip().lower() or None)
    tone_lookup = _tone_lookup(project_id, db)

    # Small/medium jobs run live, as before. Large ones go through
    # Anthropic's Message Batches API instead — cheaper per token, but the
    # AI findings aren't ready immediately (see BATCH_THRESHOLD_CHARS).
    volume = estimate_check_volume(sheets, resolved_source, target_filter)

    if volume <= BATCH_THRESHOLD_CHARS:
        results = await run_multi_check(
            sheets, resolved_source, selected_checks, extra_instructions,
            tone_lookup, target_filter,
        )
        record = models.MultiCheck(
            project_id=project_id,
            filename=file.filename or "upload.xlsx",
            source_lang=resolved_source,
            checks_run=selected_checks,
            summary=results["summary"],
            results=results,
            status="completed",
            performed_by_name=manager_name.strip(),
            manager_id=manager_id,
            cost_usd=results["summary"].get("cost_usd", 0.0),
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return {
            "multi_check_id": record.id,
            "status": "completed",
            "source_lang": resolved_source,
            "summary": results["summary"],
            "sheets": results["sheets"],
            "cost_usd": record.cost_usd,
        }

    requests, skeleton = build_batch_plan(
        sheets, resolved_source, selected_checks, extra_instructions,
        tone_lookup, target_filter,
    )
    batch_id = await submit_multi_check_batch(requests) if requests else None

    if batch_id is None:
        # Nothing to submit (no AI check types selected, or no API key
        # configured) — the rule-based skeleton is already the final answer.
        results = finalize_batch_results(skeleton, {})
        record = models.MultiCheck(
            project_id=project_id,
            filename=file.filename or "upload.xlsx",
            source_lang=resolved_source,
            checks_run=selected_checks,
            summary=results["summary"],
            results=results,
            status="completed",
            performed_by_name=manager_name.strip(),
            manager_id=manager_id,
            cost_usd=results["summary"].get("cost_usd", 0.0),
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return {
            "multi_check_id": record.id,
            "status": "completed",
            "source_lang": resolved_source,
            "summary": results["summary"],
            "sheets": results["sheets"],
            "cost_usd": record.cost_usd,
        }

    record = models.MultiCheck(
        project_id=project_id,
        filename=file.filename or "upload.xlsx",
        source_lang=resolved_source,
        checks_run=selected_checks,
        summary={},
        results={"skeleton": skeleton},
        status="processing",
        batch_id=batch_id,
        performed_by_name=manager_name.strip(),
        manager_id=manager_id,
        # Real cost isn't known until the Anthropic batch ends — see
        # multi_check_detail, which fills this in once it finalizes.
        cost_usd=0.0,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return {
        "multi_check_id": record.id,
        "status": "processing",
        "source_lang": resolved_source,
    }


@app.get(
    "/projects/{project_id}/multi-check",
    response_model=list[schemas.MultiCheckHistoryOut],
)
def multi_check_history(project_id: int, manager_id: int, db: Session = Depends(get_db)):
    """Scoped to the requesting folder only — same per-folder history
    scoping as single_check_history, above."""
    _get_project(project_id, db)
    records = db.query(models.MultiCheck).filter(
        models.MultiCheck.project_id == project_id,
        models.MultiCheck.manager_id == manager_id,
    ).order_by(models.MultiCheck.created_at.desc()).limit(50).all()
    return [
        schemas.MultiCheckHistoryOut(
            id=r.id, filename=r.filename, source_lang=r.source_lang,
            summary=r.summary, status=r.status, performed_by_name=r.performed_by_name,
            created_at=r.created_at.isoformat(),
            cost_usd=r.cost_usd,
        )
        for r in records
    ]


@app.get("/projects/{project_id}/multi-check/{multi_check_id}")
async def multi_check_detail(project_id: int, multi_check_id: int, manager_id: int, db: Session = Depends(get_db)):
    _get_project(project_id, db)
    record = db.get(models.MultiCheck, multi_check_id)
    if record is None or record.project_id != project_id or record.manager_id != manager_id:
        raise HTTPException(404, "Проверка не найдена.")

    if record.status == "processing" and record.batch_id:
        finalized = await try_finalize_batch(record.batch_id, record.results["skeleton"])
        if finalized is not None:
            record.results = finalized
            record.summary = finalized["summary"]
            record.status = "completed"
            record.cost_usd = finalized["summary"].get("cost_usd", 0.0)
            db.commit()
            db.refresh(record)

    if record.status == "processing":
        return {
            "multi_check_id": record.id,
            "status": "processing",
            "filename": record.filename,
            "source_lang": record.source_lang,
        }

    return {
        "multi_check_id": record.id,
        "status": "completed",
        "filename": record.filename,
        "source_lang": record.source_lang,
        "summary": record.summary,
        "sheets": record.results.get("sheets", []),
        "cost_usd": record.cost_usd,
    }


@app.get("/projects/{project_id}/multi-check/{multi_check_id}/report.xlsx")
def multi_check_report(project_id: int, multi_check_id: int, manager_id: int, db: Session = Depends(get_db)):
    _get_project(project_id, db)
    record = db.get(models.MultiCheck, multi_check_id)
    if record is None or record.project_id != project_id or record.manager_id != manager_id:
        raise HTTPException(404, "Проверка не найдена.")
    if record.status != "completed":
        raise HTTPException(409, "Проверка ещё обрабатывается — отчёт будет доступен после завершения.")

    report_bytes = build_report_workbook(record.filename, record.source_lang, record.results)
    filename = f"qa-report-{record.id}.xlsx"
    return StreamingResponse(
        iter([report_bytes]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
