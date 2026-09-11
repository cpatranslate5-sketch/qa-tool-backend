from fastapi import Depends, FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import hash_code, verify_code
from app.claude_client import run_ai_checks
from app.config import settings
from app.database import get_db, init_db
from app.excel_multi import build_report_workbook, parse_workbook, pick_source_lang, run_multi_check
from app.glossary import format_glossary_prompt, parse_glossary_workbook, terms_for_language
from app.rule_checks import run_rule_checks

app = FastAPI(title="Translation QA Tool", version="0.3.0")

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
# admin folder can create projects, add language folders, or edit a
# project's glossary. Every folder can use whatever already exists.

@app.get("/managers", response_model=list[schemas.ManagerOut])
def list_managers(db: Session = Depends(get_db)):
    return db.query(models.Manager).order_by(models.Manager.id.asc()).all()


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
# may create one or restructure it (language folders, glossary).

@app.get("/projects", response_model=list[schemas.ProjectOut])
def list_projects(db: Session = Depends(get_db)):
    return db.query(models.Project).order_by(models.Project.name).all()


# Every new project starts out with this fixed set of language folders,
# so the admin doesn't have to add each one by hand. More can still be
# added afterwards via /projects/{id}/languages for anything not covered
# here.
DEFAULT_PROJECT_LANGUAGES = [
    "en", "ar", "az", "bd", "de", "el", "es", "es-mx", "es-ar", "fr-ci",
    "hi", "hing", "id", "it", "jp", "kz", "ko", "kg", "mr", "ms", "pl",
    "pt-br", "ro", "ru", "sw", "te", "tj", "th", "tl", "tr", "ua", "ur",
    "uz", "vi", "cn",
]


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
    for code in DEFAULT_PROJECT_LANGUAGES:
        db.add(models.ProjectLanguage(project_id=project.id, lang_code=code))
    db.commit()
    db.refresh(project)
    return project


def _get_project(project_id: int, db: Session) -> models.Project:
    project = db.get(models.Project, project_id)
    if project is None:
        raise HTTPException(404, "Проект не найден.")
    return project


def _all_glossary_terms(project_id: int, db: Session) -> list[dict]:
    rows = db.query(models.GlossaryTerm).filter(
        models.GlossaryTerm.project_id == project_id
    ).order_by(models.GlossaryTerm.row_order).all()
    return [{"term_en": t.term_en, "description": t.description, "translations": t.translations} for t in rows]


def _glossary_lookup(project_id: int, db: Session):
    """Returns a callable(lang_code) -> glossary prompt text, narrowed to
    EN + RU + that one language — every check (single or multi) uses this
    rather than ever loading the full multi-language table."""
    all_terms = _all_glossary_terms(project_id, db)

    def lookup(lang_code: str) -> str:
        rows = terms_for_language(all_terms, lang_code)
        return format_glossary_prompt(rows, lang_code)

    return lookup


@app.post("/projects/{project_id}/glossary/upload", response_model=schemas.GlossaryStatusOut)
async def upload_glossary(
    project_id: int,
    file: UploadFile = File(...),
    manager_id: int = Form(...),
    db: Session = Depends(get_db),
):
    """Admin-only. Replaces the project's whole glossary with the uploaded
    file — one sheet, EN column, optional description column, then one
    column per target language (matches the agency's existing doc)."""
    _require_admin(manager_id, db)
    project = _get_project(project_id, db)
    file_bytes = await file.read()

    try:
        terms = parse_glossary_workbook(file_bytes)
    except Exception:
        raise HTTPException(400, "Не удалось прочитать файл — убедитесь, что это .xlsx в формате глоссария.")
    if not terms:
        raise HTTPException(400, "В файле не найдено ни одного термина (пустая колонка EN?).")

    db.query(models.GlossaryTerm).filter(models.GlossaryTerm.project_id == project_id).delete()
    for i, t in enumerate(terms):
        db.add(models.GlossaryTerm(
            project_id=project_id,
            term_en=t["term_en"],
            description=t["description"],
            translations=t["translations"],
            row_order=i,
        ))
    project.glossary_filename = file.filename or "glossary.xlsx"
    project.glossary_uploaded_at = models._now()
    db.commit()

    return schemas.GlossaryStatusOut(
        filename=project.glossary_filename, uploaded_at=project.glossary_uploaded_at, term_count=len(terms)
    )


@app.get("/projects/{project_id}/glossary/status", response_model=schemas.GlossaryStatusOut)
def glossary_status(project_id: int, db: Session = Depends(get_db)):
    """Visible to every folder — just enough to see what's loaded, not the
    full table."""
    project = _get_project(project_id, db)
    term_count = db.query(models.GlossaryTerm).filter(models.GlossaryTerm.project_id == project_id).count()
    return schemas.GlossaryStatusOut(
        filename=project.glossary_filename, uploaded_at=project.glossary_uploaded_at, term_count=term_count
    )


# ----------------------------------------------------------- languages ----

@app.get("/projects/{project_id}/languages", response_model=list[schemas.LanguageOut])
def list_languages(project_id: int, db: Session = Depends(get_db)):
    _get_project(project_id, db)
    return db.query(models.ProjectLanguage).filter(
        models.ProjectLanguage.project_id == project_id
    ).order_by(models.ProjectLanguage.lang_code).all()


@app.post("/projects/{project_id}/languages", response_model=schemas.LanguageOut)
def add_language(project_id: int, payload: schemas.LanguageIn, db: Session = Depends(get_db)):
    _require_admin(payload.manager_id, db)
    _get_project(project_id, db)
    code = payload.lang_code.strip().lower()
    if not code:
        raise HTTPException(400, "Укажите код языка.")
    exists = db.query(models.ProjectLanguage).filter(
        models.ProjectLanguage.project_id == project_id, models.ProjectLanguage.lang_code == code
    ).first()
    if exists:
        return exists
    lang = models.ProjectLanguage(project_id=project_id, lang_code=code)
    db.add(lang)
    db.commit()
    db.refresh(lang)
    return lang


# --------------------------------------------------------- single check ---
# Any folder may run checks — only structural changes above are admin-only.

@app.post("/check", response_model=schemas.CheckOut)
async def check(payload: schemas.CheckIn, db: Session = Depends(get_db)):
    if not payload.source.strip() or not payload.translation.strip():
        return schemas.CheckOut(findings=[])

    glossary = payload.glossary
    language = None
    if payload.project_id and payload.language_id:
        project = db.get(models.Project, payload.project_id)
        language = db.get(models.ProjectLanguage, payload.language_id)
        if project is None or language is None or language.project_id != project.id:
            raise HTTPException(404, "Проект или языковая папка не найдены.")
        # Narrowed to EN + RU + this one language — never the whole glossary.
        glossary = _glossary_lookup(project.id, db)(language.lang_code)

    findings = run_rule_checks(payload.source, payload.translation, payload.checks)
    findings += await run_ai_checks(
        payload.source, payload.translation, glossary, payload.checks, payload.extra_instructions
    )

    single_check_id = None
    if language is not None:
        record = models.SingleCheck(
            project_id=language.project_id,
            language_id=language.id,
            source=payload.source,
            translation=payload.translation,
            checks_run=payload.checks,
            findings=findings,
            performed_by_name=payload.manager_name.strip(),
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        single_check_id = record.id

    return schemas.CheckOut(findings=findings, single_check_id=single_check_id)


@app.get(
    "/projects/{project_id}/languages/{language_id}/history",
    response_model=list[schemas.SingleCheckHistoryOut],
)
def single_check_history(project_id: int, language_id: int, db: Session = Depends(get_db)):
    _get_project(project_id, db)
    records = db.query(models.SingleCheck).filter(
        models.SingleCheck.project_id == project_id, models.SingleCheck.language_id == language_id
    ).order_by(models.SingleCheck.created_at.desc()).limit(50).all()
    return [
        schemas.SingleCheckHistoryOut(
            id=r.id, source=r.source, translation=r.translation,
            checks_run=r.checks_run, findings=r.findings,
            performed_by_name=r.performed_by_name,
            created_at=r.created_at.isoformat(),
        )
        for r in records
    ]


# ---------------------------------------------------------- multi check ---

DEFAULT_MULTI_CHECKS = [
    "numbers", "placeholders", "max_length", "glossary", "register", "typo",
    "untranslatable", "completeness", "punctuation",
]


@app.post("/projects/{project_id}/multi-check")
async def multi_check(
    project_id: int,
    file: UploadFile = File(...),
    source_lang: str = Form(""),
    manager_name: str = Form(""),
    extra_instructions: str = Form(""),
    # Comma-separated check keys from the UI's checkboxes; empty/absent falls
    # back to the full default set.
    checks: str = Form(""),
    db: Session = Depends(get_db),
):
    project = _get_project(project_id, db)
    file_bytes = await file.read()

    try:
        sheets = parse_workbook(file_bytes)
    except Exception:
        raise HTTPException(400, "Не удалось прочитать файл — убедитесь, что это .xlsx с языковыми колонками.")

    if not sheets:
        raise HTTPException(400, "В файле не найдено ни одной колонки с кодом языка.")

    selected_checks = [c.strip() for c in checks.split(",") if c.strip()] or DEFAULT_MULTI_CHECKS

    resolved_source = pick_source_lang(sheets, source_lang.strip().lower() or None)
    results = await run_multi_check(
        sheets, resolved_source, _glossary_lookup(project_id, db), selected_checks, extra_instructions
    )

    record = models.MultiCheck(
        project_id=project_id,
        filename=file.filename or "upload.xlsx",
        source_lang=resolved_source,
        checks_run=selected_checks,
        summary=results["summary"],
        results=results,
        performed_by_name=manager_name.strip(),
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    return {
        "multi_check_id": record.id,
        "source_lang": resolved_source,
        "summary": results["summary"],
        "sheets": results["sheets"],
    }


@app.get(
    "/projects/{project_id}/multi-check",
    response_model=list[schemas.MultiCheckHistoryOut],
)
def multi_check_history(project_id: int, db: Session = Depends(get_db)):
    _get_project(project_id, db)
    records = db.query(models.MultiCheck).filter(
        models.MultiCheck.project_id == project_id
    ).order_by(models.MultiCheck.created_at.desc()).limit(50).all()
    return [
        schemas.MultiCheckHistoryOut(
            id=r.id, filename=r.filename, source_lang=r.source_lang,
            summary=r.summary, performed_by_name=r.performed_by_name,
            created_at=r.created_at.isoformat(),
        )
        for r in records
    ]


@app.get("/projects/{project_id}/multi-check/{multi_check_id}")
def multi_check_detail(project_id: int, multi_check_id: int, db: Session = Depends(get_db)):
    _get_project(project_id, db)
    record = db.get(models.MultiCheck, multi_check_id)
    if record is None or record.project_id != project_id:
        raise HTTPException(404, "Проверка не найдена.")
    return {
        "multi_check_id": record.id,
        "filename": record.filename,
        "source_lang": record.source_lang,
        "summary": record.summary,
        "sheets": record.results.get("sheets", []),
    }


@app.get("/projects/{project_id}/multi-check/{multi_check_id}/report.xlsx")
def multi_check_report(project_id: int, multi_check_id: int, db: Session = Depends(get_db)):
    _get_project(project_id, db)
    record = db.get(models.MultiCheck, multi_check_id)
    if record is None or record.project_id != project_id:
        raise HTTPException(404, "Проверка не найдена.")

    report_bytes = build_report_workbook(record.filename, record.source_lang, record.results)
    filename = f"qa-report-{record.id}.xlsx"
    return StreamingResponse(
        iter([report_bytes]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
