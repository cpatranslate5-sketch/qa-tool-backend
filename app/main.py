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
from app.rule_checks import run_rule_checks

app = FastAPI(title="Translation QA Tool", version="0.2.0")

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


# ---------------------------------------------------------------- auth ----

@app.post("/auth/login", response_model=schemas.LoginOut)
def login(payload: schemas.LoginIn, db: Session = Depends(get_db)):
    name = payload.name.strip()
    code = payload.code.strip()
    if not name or not code:
        raise HTTPException(400, "Введите имя и код.")

    manager = db.query(models.Manager).filter(models.Manager.name == name).first()
    if manager is None:
        manager = models.Manager(name=name, code_hash=hash_code(code))
        db.add(manager)
        db.commit()
        db.refresh(manager)
        return schemas.LoginOut(manager_id=manager.id, name=manager.name, is_new=True)

    if not verify_code(code, manager.code_hash):
        raise HTTPException(401, "Неверный код для этого имени.")
    return schemas.LoginOut(manager_id=manager.id, name=manager.name, is_new=False)


def _get_manager(manager_id: int, db: Session) -> models.Manager:
    manager = db.get(models.Manager, manager_id)
    if manager is None:
        raise HTTPException(401, "Менеджер не найден — войдите заново.")
    return manager


def _get_project(project_id: int, manager_id: int, db: Session) -> models.Project:
    project = db.get(models.Project, project_id)
    if project is None or project.manager_id != manager_id:
        raise HTTPException(404, "Проект не найден.")
    return project


# ------------------------------------------------------------ projects ----

@app.get("/managers/{manager_id}/projects", response_model=list[schemas.ProjectOut])
def list_projects(manager_id: int, db: Session = Depends(get_db)):
    _get_manager(manager_id, db)
    return db.query(models.Project).filter(models.Project.manager_id == manager_id).order_by(models.Project.name).all()


@app.post("/managers/{manager_id}/projects", response_model=schemas.ProjectOut)
def create_project(manager_id: int, payload: schemas.ProjectIn, db: Session = Depends(get_db)):
    _get_manager(manager_id, db)
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Введите название проекта.")
    exists = db.query(models.Project).filter(
        models.Project.manager_id == manager_id, models.Project.name == name
    ).first()
    if exists:
        raise HTTPException(409, "Проект с таким названием уже есть.")
    project = models.Project(manager_id=manager_id, name=name)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@app.put("/managers/{manager_id}/projects/{project_id}/glossary", response_model=schemas.ProjectOut)
def update_glossary(manager_id: int, project_id: int, payload: schemas.GlossaryIn, db: Session = Depends(get_db)):
    project = _get_project(project_id, manager_id, db)
    project.glossary = payload.glossary
    db.commit()
    db.refresh(project)
    return project


# ----------------------------------------------------------- languages ----

@app.get("/managers/{manager_id}/projects/{project_id}/languages", response_model=list[schemas.LanguageOut])
def list_languages(manager_id: int, project_id: int, db: Session = Depends(get_db)):
    _get_project(project_id, manager_id, db)
    return db.query(models.ProjectLanguage).filter(
        models.ProjectLanguage.project_id == project_id
    ).order_by(models.ProjectLanguage.lang_code).all()


@app.post("/managers/{manager_id}/projects/{project_id}/languages", response_model=schemas.LanguageOut)
def add_language(manager_id: int, project_id: int, payload: schemas.LanguageIn, db: Session = Depends(get_db)):
    _get_project(project_id, manager_id, db)
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
        glossary = project.glossary

    findings = run_rule_checks(payload.source, payload.translation, payload.checks)
    findings += await run_ai_checks(payload.source, payload.translation, glossary, payload.checks)

    single_check_id = None
    if language is not None:
        record = models.SingleCheck(
            project_id=language.project_id,
            language_id=language.id,
            source=payload.source,
            translation=payload.translation,
            checks_run=payload.checks,
            findings=findings,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        single_check_id = record.id

    return schemas.CheckOut(findings=findings, single_check_id=single_check_id)


@app.get(
    "/managers/{manager_id}/projects/{project_id}/languages/{language_id}/history",
    response_model=list[schemas.SingleCheckHistoryOut],
)
def single_check_history(manager_id: int, project_id: int, language_id: int, db: Session = Depends(get_db)):
    _get_project(project_id, manager_id, db)
    records = db.query(models.SingleCheck).filter(
        models.SingleCheck.project_id == project_id, models.SingleCheck.language_id == language_id
    ).order_by(models.SingleCheck.created_at.desc()).limit(50).all()
    return [
        schemas.SingleCheckHistoryOut(
            id=r.id, source=r.source, translation=r.translation,
            checks_run=r.checks_run, findings=r.findings,
            created_at=r.created_at.isoformat(),
        )
        for r in records
    ]


# ---------------------------------------------------------- multi check ---

DEFAULT_MULTI_CHECKS = ["numbers", "placeholders", "max_length", "glossary", "register", "typo"]


@app.post("/managers/{manager_id}/projects/{project_id}/multi-check")
async def multi_check(
    manager_id: int,
    project_id: int,
    file: UploadFile = File(...),
    source_lang: str = Form(""),
    db: Session = Depends(get_db),
):
    project = _get_project(project_id, manager_id, db)
    file_bytes = await file.read()

    try:
        sheets = parse_workbook(file_bytes)
    except Exception:
        raise HTTPException(400, "Не удалось прочитать файл — убедитесь, что это .xlsx с языковыми колонками.")

    if not sheets:
        raise HTTPException(400, "В файле не найдено ни одной колонки с кодом языка.")

    resolved_source = pick_source_lang(sheets, source_lang.strip().lower() or None)
    results = await run_multi_check(sheets, resolved_source, project.glossary, DEFAULT_MULTI_CHECKS)

    record = models.MultiCheck(
        project_id=project_id,
        filename=file.filename or "upload.xlsx",
        source_lang=resolved_source,
        checks_run=DEFAULT_MULTI_CHECKS,
        summary=results["summary"],
        results=results,
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
    "/managers/{manager_id}/projects/{project_id}/multi-check",
    response_model=list[schemas.MultiCheckHistoryOut],
)
def multi_check_history(manager_id: int, project_id: int, db: Session = Depends(get_db)):
    _get_project(project_id, manager_id, db)
    records = db.query(models.MultiCheck).filter(
        models.MultiCheck.project_id == project_id
    ).order_by(models.MultiCheck.created_at.desc()).limit(50).all()
    return [
        schemas.MultiCheckHistoryOut(
            id=r.id, filename=r.filename, source_lang=r.source_lang,
            summary=r.summary, created_at=r.created_at.isoformat(),
        )
        for r in records
    ]


@app.get("/managers/{manager_id}/projects/{project_id}/multi-check/{multi_check_id}")
def multi_check_detail(manager_id: int, project_id: int, multi_check_id: int, db: Session = Depends(get_db)):
    _get_project(project_id, manager_id, db)
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


@app.get("/managers/{manager_id}/projects/{project_id}/multi-check/{multi_check_id}/report.xlsx")
def multi_check_report(manager_id: int, project_id: int, multi_check_id: int, db: Session = Depends(get_db)):
    _get_project(project_id, manager_id, db)
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
