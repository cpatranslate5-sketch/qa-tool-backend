import datetime
import secrets
import logging

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import hash_code, verify_code
from app.claude_client import domain_note_for_names, route_for_lang, run_ai_checks
from app.share_page import ALL_LANGS, numbered_findings, report_langs, pending_keys, sent_keys, render_not_found, render_shared_report, share_page_headers
from app.config import settings
from app.database import SessionLocal, get_db, init_db
from app.excel_multi import (
    BATCH_THRESHOLD_CHARS,
    _label_to_code,
    _normalize_lang_label,
    apply_second_opinion,
    build_batch_plan,
    build_report_workbook,
    cancel_multi_check_batch,
    estimate_check_volume,
    lang_matches_exactly,
    finalize_batch_results,
    merge_lang_codes,
    parse_workbook,
    pick_source_lang,
    resolve_lang_code,
    run_multi_check,
    submit_multi_check_batch,
    try_finalize_batch,
)
from app.model_comparison import run_chunk_size_comparison, run_model_comparison
from app.rule_checks import run_rule_checks
from app import styleguide as sg_mod

logger = logging.getLogger(__name__)

app = FastAPI(title="Translation QA Tool", version="0.4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Every AI-backed check (single, multi live, multi batch submit, batch
# status/results polling) calls out to Anthropic via httpx and can fail for
# reasons outside our control — a transient outage, a rate limit, a bad
# response. Without this handler, such a failure is an UNHANDLED exception,
# and Starlette's default handling for those returns a bare 500 built by
# the outermost ServerErrorMiddleware — which sits OUTSIDE CORSMiddleware,
# so the response never gets an Access-Control-Allow-Origin header. The
# browser then reports this as "blocked by CORS policy", hiding the real
# cause entirely (confirmed by direct testing: this exact confusing error
# is what a manager saw after uploading a file whose language column had a
# Cyrillic character Anthropic's API rejected — a batch-submission crash,
# not an actual CORS misconfiguration).
#
# The fix is this handler, registered for httpx.HTTPError specifically
# (the base class of both bad-status responses and connection/timeout
# failures) rather than the bare Exception class: FastAPI/Starlette special-
# cases a handler registered for Exception (or code 500) by routing it to
# that same outer ServerErrorMiddleware, so it would ALSO lose the CORS
# header — verified experimentally. A handler for a specific exception type
# like this one is instead run by ExceptionMiddleware, which sits INSIDE
# CORSMiddleware, so its response correctly gets the header. Genuinely
# unexpected bugs (not an Anthropic/network failure) still crash with the
# framework's normal bare 500 — deliberately not swallowed here, since
# those need fixing, not a friendly message papering over them.
@app.exception_handler(httpx.HTTPError)
async def anthropic_call_failed(request: Request, exc: httpx.HTTPError):
    # %r on exc alone never showed the actual reason Anthropic rejected the
    # request (httpx.HTTPStatusError's own string form is just the status
    # code and URL) — logging the response body too, when there is one,
    # surfaces Anthropic's own {"error": {"type": ..., "message": ...}}
    # detail, which is the only way to tell a real invalid-request bug on
    # our side apart from an account/key/billing problem on Anthropic's.
    body = ""
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            body = f" | response body: {exc.response.text}"
        except Exception:
            pass
    logger.error("Anthropic API call failed on %s %s: %r%s", request.method, request.url.path, exc, body)
    # A bad/expired ANTHROPIC_API_KEY surfaces as httpx.HTTPStatusError with
    # a 401/403 — that's a config problem on our side, not a transient
    # Anthropic outage, so telling the user to just "try again in a minute"
    # would be actively misleading (and would hide a real, fixable problem
    # behind a retry loop). Give that case its own message; everything else
    # (connection errors, timeouts, 5xx, rate limits) keeps the generic one.
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
        detail = (
            "Сервис ИИ-проверки отклонил запрос из-за ошибки авторизации — "
            "похоже, дело не во временном сбое, а в настройках ключа доступа. "
            "Сообщите нам, это на нашей стороне."
        )
    else:
        detail = (
            "Не удалось связаться с сервисом ИИ-проверки — похоже, временный сбой. "
            "Попробуйте ещё раз через минуту; если не поможет — сообщите нам."
        )
    return JSONResponse(status_code=502, content={"detail": detail})


@app.on_event("startup")
def on_startup():
    init_db()
    db = SessionLocal()
    try:
        _seed_clients(db)
    except Exception:
        logger.exception("client/styleguide seeding failed")
        db.rollback()
    finally:
        db.close()


def _seed_clients(db: Session) -> None:
    """Creates the «1win» client with its styleguide and its seven projects
    once (2026-10-04, Александр). Existing projects with the same names
    (case-insensitive) are moved into the client instead of duplicated.
    Never runs again once the client exists — later edits are the admin's."""
    existing_client = db.query(models.Client).filter(models.Client.name == "1win").first()
    if existing_client is not None:
        _patch_vi_ranges(existing_client, db)
        return
    client = models.Client(name="1win", styleguide=sg_mod.build_client_seed())
    db.add(client)
    db.flush()
    existing = {p.name.strip().lower(): p for p in db.query(models.Project).all()}
    for name, overrides in sg_mod.PROJECTS_SEED:
        project = existing.get(name.lower())
        if project is None:
            project = models.Project(name=name, created_by_name="Система")
            db.add(project)
        project.client_id = client.id
        if overrides and not project.styleguide:
            project.styleguide = overrides
    db.commit()


def _patch_vi_ranges(client: models.Client, db: Session) -> None:
    """2026-10-04 (Александр): Vietnamese ranges without spaces. Applied to
    an already-seeded client only while its Vietnamese settings are still
    the untouched seed values."""
    data = dict(client.styleguide or {})
    vi = dict(data.get("vi") or {})
    auto = dict(vi.get("auto") or {})
    if not auto or auto.get("ranges") != "any" or auto.get("range_spaces") != "any":
        return
    seed_vi = sg_mod.build_client_seed().get("vi") or {}
    auto["ranges"], auto["range_spaces"] = "en_or_hyphen", "none"
    vi["auto"] = auto
    text = ((vi.get("en_dash") or {}).get("text") or "")
    if "Уточнение: диапазоны" not in text and seed_vi.get("en_dash"):
        vi["en_dash"] = seed_vi["en_dash"]
    data["vi"] = vi
    client.styleguide = data
    db.commit()


def _project_styleguide(project: models.Project | None) -> dict | None:
    """The effective styleguide for checks in this project (client's rules
    with the project's own sections on top), or None without a client."""
    if project is None or project.client_id is None:
        return None
    client = project.client
    if client is None:
        return None
    return sg_mod.effective_rules(client.styleguide, project.styleguide)


async def _run_second_opinion_background(multi_check_id: int, results: dict) -> None:
    """Runs the automatic Sonnet-only second-opinion pass (apply_second_opinion)
    AFTER a multi-check's response has already been sent to the browser,
    instead of blocking that response on it.

    Why: apply_second_opinion makes one more API call per language on top
    of the check's own AI calls. Blocking the request on
    it made the whole thing slow enough that the browser's fetch sometimes
    gave up ("Failed to fetch") even though the backend was still working
    and the result was saved fine a moment later — genuinely confusing
    (was it saved? do I re-run it and pay twice?) and, with this change,
    the live/"nothing to submit" response and the batch-finalize response
    all return as soon as the check itself is done, same as before this
    feature existed. The frontend polls multi-check detail until
    second_opinion_pending flips back to false (same pattern it already
    uses for a still-processing batch job) — see CheckRunner.tsx.

    Runs in its OWN DB session: a FastAPI background task starts only
    after the response has been sent, by which point the request's own
    `Depends(get_db)` session is already closed.

    Any failure here (an unexpected bug — apply_second_opinion already
    turns a single model-call failure into a per-language warning on its
    own, see its own docstring) just clears second_opinion_pending without
    attaching any percents. shouldKeepFinding on the frontend already
    treats a missing percent as "can't safely judge this, always keep
    it" — so worst case, "Отфильтровать отчёт" just doesn't drop anything
    for this check. It never silently double-runs or double-charges: the
    check itself already ran and was saved before this task was even
    scheduled — this only ever adds the two extra opinion calls once."""
    try:
        results = await apply_second_opinion(results)
    except Exception:
        logger.exception("apply_second_opinion failed for multi_check_id=%s", multi_check_id)
    results["second_opinion_pending"] = False

    db = SessionLocal()
    try:
        record = db.get(models.MultiCheck, multi_check_id)
        if record is not None:
            record.results = results
            record.summary = results.get("summary", record.summary)
            record.cost_usd = results.get("summary", {}).get("cost_usd", record.cost_usd)
            db.commit()
    finally:
        db.close()


async def _run_live_check_background(
    multi_check_id: int,
    sheets: list[dict],
    resolved_source: str,
    selected_checks: list[str],
    extra_instructions: str,
    target_filter: set[str] | None,
    styleguide: dict | None = None,
) -> None:
    """Runs the actual AI check (run_multi_check) for the live/"Срочно" path
    AFTER the initial request has already returned a "processing" response —
    added 2026-09-26. Before this, that branch did `await run_multi_check(...)`
    directly inside the request handler, so a medium/large document (with
    or without "Срочно") could block the response long enough for the
    browser's own fetch to give up ("Failed to fetch") — the check still
    finished and saved correctly on the server a while later (that's why it
    then showed up fine in history), which is exactly what Александр
    reported kept happening even after the second-opinion pass right above
    was already moved to the background. This is that same fix, applied one
    step earlier in the pipeline: the record starts as "processing" (see the
    multi_check endpoint) with no batch_id — nothing was actually handed to
    Anthropic's batch queue, this is still the live/full-price path, just no
    longer blocking the request — and multi_check_detail's own polling
    branch picks it up the same way it already does for a genuine batch job,
    distinguished only by the "batch" flag in the response (see
    CheckRunner.tsx, which uses it to pick a much faster poll interval here
    since there's no external Anthropic queue to be polite to).

    Runs in its OWN DB session, same as _run_second_opinion_background right
    above — the request's own session is already closed by the time a
    background task gets to run.

    On success, chains straight into _run_second_opinion_background so a
    live check goes through the exact same two-stage background flow a
    batch job already does. On failure (an Anthropic outage, an unexpected
    bug), marks the record "failed" with a short human-readable message
    instead of leaving it stuck on "processing" forever with no explanation
    — see multi_check_detail's "failed" branch and CheckRunner.tsx's
    matching render block."""
    db = SessionLocal()
    try:
        try:
            results = await run_multi_check(
                sheets, resolved_source, selected_checks, extra_instructions, target_filter,
                styleguide=styleguide,
            )
        except Exception:
            logger.exception("run_multi_check failed in background for multi_check_id=%s", multi_check_id)
            record = db.get(models.MultiCheck, multi_check_id)
            if record is not None:
                record.status = "failed"
                record.results = {"error": "Не удалось выполнить проверку — попробуйте ещё раз."}
                record.completed_at = datetime.datetime.now(datetime.timezone.utc)
                db.commit()
            return

        # 2026-09-29 redesign: no separate second-opinion pass any more — the
        # checking model already rated (and the backend already filtered)
        # its own findings, so the check is final the moment it's saved.
        results["second_opinion_pending"] = False
        record = db.get(models.MultiCheck, multi_check_id)
        if record is None:
            # Manager deleted/cancelled it while this was still running (see
            # delete_multi_check) — nothing left to save into.
            return
        record.summary = results["summary"]
        record.results = results
        record.status = "completed"
        record.cost_usd = results["summary"].get("cost_usd", 0.0)
        record.completed_at = datetime.datetime.now(datetime.timezone.utc)
        db.commit()
    finally:
        db.close()


def _with_domain_note(
    extra_instructions: str, *names: str | None, project_description: str | None = None,
) -> str:
    """Builds the task's "Особые указания" block that goes into every AI
    prompt: the project's own admin-written description (see
    models.Project.description), the built-in subject-domain note (e.g.
    betting for the "1win" folder/project — see
    claude_client.domain_note_for_names), then the task's own instructions."""
    parts = []
    desc = (project_description or "").strip()
    if desc:
        parts.append(
            "ОПИСАНИЕ ПРОЕКТА (написано менеджером проекта — учитывай тематику, аудиторию и требования при "
            f"оценке каждой строки):\n{desc}"
        )
    note = domain_note_for_names(*names)
    if note:
        parts.append(note)
    extra = (extra_instructions or "").strip()
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)


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


def _load_alias_map(db: Session) -> dict[str, str]:
    """The GLOBAL "this raw spelling means this language" dictionary (see
    models.LanguageAlias and app.excel_multi._label_to_code), fetched
    fresh from the DB on every request that parses a file or a manually-
    typed language code — it's a small table, and always reflecting the
    latest additions/removals matters more here than caching it."""
    return {
        row[0]: row[1]
        for row in db.query(models.LanguageAlias.alias, models.LanguageAlias.canonical_code).all()
    }


# -------------------------------------------------------------- projects ----
# Shared/global: every folder sees the same projects. Only the admin folder
# may create, delete, or restructure one. No more per-language
# sub-folders, and (as of 2026-09-16) no more reference documents either —
# see models.Project's docstring for what used to live here.

@app.get("/projects", response_model=list[schemas.ProjectOut])
def list_projects(db: Session = Depends(get_db)):
    return db.query(models.Project).order_by(models.Project.name).all()


def _copy_project_documents(from_project_id: int, to_project_id: int, db: Session) -> None:
    """Deep-copies the language catalog from one project into another as an
    independent starting point — editing the new project's copy afterward
    (adding/removing a catalog language) never touches the original.

    Used to also copy tone-of-address rows, back when that document
    existed (removed 2026-09-16 — see models.Project's docstring)."""
    from_project = db.get(models.Project, from_project_id)
    if from_project is None:
        raise HTTPException(404, "Проект-образец не найден.")

    for r in db.query(models.LanguageCatalogEntry).filter(models.LanguageCatalogEntry.project_id == from_project_id).all():
        db.add(models.LanguageCatalogEntry(project_id=to_project_id, lang_code=r.lang_code))


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
        src = db.get(models.Project, payload.copy_from_project_id)
        if src is not None and src.description:
            project.description = src.description

    db.commit()
    db.refresh(project)
    return project


PROJECT_DESCRIPTION_MAX_CHARS = 4000


@app.get("/projects/{project_id}", response_model=schemas.ProjectOut)
def get_project(project_id: int, db: Session = Depends(get_db)):
    return _get_project(project_id, db)


@app.put("/projects/{project_id}/description", response_model=schemas.ProjectOut)
def update_project_description(project_id: int, payload: schemas.ProjectDescriptionIn, db: Session = Depends(get_db)):
    """Admin-only: the project's free-text description (subject area,
    audience, special requirements), sent to the AI with every check here."""
    _require_admin(payload.manager_id, db)
    project = _get_project(project_id, db)
    text_ = (payload.description or "").strip()
    if len(text_) > PROJECT_DESCRIPTION_MAX_CHARS:
        raise HTTPException(400, f"Описание слишком длинное — максимум {PROJECT_DESCRIPTION_MAX_CHARS} символов.")
    project.description = text_
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


# ---------------------------------------------------- clients/styleguides ----
# 2026-10-04 (Александр): «Заказчики» hold projects and a styleguide; every
# project inherits it and may override single sections per language. Anyone
# can view; only the admin folder edits. Each edit is logged (with «Вернуть»).

def _client_out(c: models.Client) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "projects": [{"id": p.id, "name": p.name} for p in sorted(c.projects, key=lambda p: p.name.lower())],
    }


@app.get("/clients")
def list_clients(db: Session = Depends(get_db)):
    return [_client_out(c) for c in db.query(models.Client).order_by(models.Client.name).all()]


@app.post("/clients")
def create_client(payload: schemas.ClientIn, db: Session = Depends(get_db)):
    _require_admin(payload.manager_id, db)
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Введите название заказчика.")
    if any(c.name.strip().lower() == name.lower() for c in db.query(models.Client).all()):
        raise HTTPException(409, "Заказчик с таким названием уже есть.")
    client = models.Client(name=name, styleguide={})
    db.add(client)
    db.commit()
    db.refresh(client)
    return _client_out(client)


@app.put("/projects/{project_id}/client")
def set_project_client(project_id: int, payload: schemas.ProjectClientIn, db: Session = Depends(get_db)):
    _require_admin(payload.manager_id, db)
    project = _get_project(project_id, db)
    if payload.client_id is not None and db.get(models.Client, payload.client_id) is None:
        raise HTTPException(404, "Заказчик не найден.")
    project.client_id = payload.client_id
    db.commit()
    return {"ok": True, "client_id": project.client_id}


def _get_client(client_id: int, db: Session) -> models.Client:
    client = db.get(models.Client, client_id)
    if client is None:
        raise HTTPException(404, "Заказчик не найден.")
    return client


def _change_out(ch: models.StyleguideChange) -> dict:
    return {
        "id": ch.id,
        "lang": ch.lang,
        "lang_label": sg_mod.LANG_LABEL.get(ch.lang, ch.lang),
        "section": ch.section,
        "section_title": sg_mod.SECTION_TITLE.get(ch.section, ch.section),
        "before": ch.value_before,
        "after": ch.value_after,
        "changed_by_name": ch.changed_by_name,
        "created_at": ch.created_at.isoformat() if ch.created_at else None,
    }


def _history(db: Session, client_id: int | None, project_id: int | None) -> list[dict]:
    q = db.query(models.StyleguideChange)
    if project_id is not None:
        q = q.filter(models.StyleguideChange.project_id == project_id)
    else:
        q = q.filter(models.StyleguideChange.client_id == client_id, models.StyleguideChange.project_id.is_(None))
    return [_change_out(c) for c in q.order_by(models.StyleguideChange.id.desc()).limit(200).all()]


@app.get("/styleguide/meta")
def styleguide_meta():
    return sg_mod.meta()


@app.get("/clients/{client_id}/styleguide")
def get_client_styleguide(client_id: int, db: Session = Depends(get_db)):
    client = _get_client(client_id, db)
    return {
        "client": _client_out(client),
        "rules": client.styleguide or {},
        "history": _history(db, client.id, None),
    }


@app.get("/projects/{project_id}/styleguide")
def get_project_styleguide(project_id: int, db: Session = Depends(get_db)):
    project = _get_project(project_id, db)
    client = project.client
    return {
        "project": {"id": project.id, "name": project.name},
        "client": _client_out(client) if client else None,
        "client_rules": (client.styleguide if client else None) or {},
        "own": project.styleguide or {},
        "history": _history(db, project.client_id, project.id),
    }


def _apply_section(container: dict | None, lang: str, section: str, value: dict | None) -> tuple[dict, dict | None]:
    data = dict(container or {})
    lang_rules = dict(data.get(lang) or {})
    before = lang_rules.get(section)
    if value is None:
        lang_rules.pop(section, None)
    else:
        lang_rules[section] = value
    if lang_rules:
        data[lang] = lang_rules
    else:
        data.pop(lang, None)
    return data, before


def _validated(payload: schemas.StyleguideSectionIn, allow_none: bool) -> dict | None:
    if payload.lang not in sg_mod.LANG_LABEL:
        raise HTTPException(400, "Неизвестный язык.")
    if payload.section not in sg_mod.SECTION_KEYS:
        raise HTTPException(400, "Неизвестный раздел.")
    if payload.value is None:
        if not allow_none:
            raise HTTPException(400, "Пустое значение.")
        return None
    if not sg_mod.section_is_valid(payload.section, payload.value):
        raise HTTPException(400, "Неверное значение раздела.")
    return sg_mod.clean_section(payload.section, payload.value)


@app.put("/clients/{client_id}/styleguide")
def update_client_styleguide(client_id: int, payload: schemas.StyleguideSectionIn, db: Session = Depends(get_db)):
    manager = _require_admin(payload.manager_id, db)
    client = _get_client(client_id, db)
    value = _validated(payload, allow_none=False)
    client.styleguide, before = _apply_section(client.styleguide, payload.lang, payload.section, value)
    if before != value:
        db.add(models.StyleguideChange(
            client_id=client.id, project_id=None, lang=payload.lang, section=payload.section,
            value_before=before, value_after=value, changed_by_name=manager.name,
        ))
    db.commit()
    return get_client_styleguide(client_id, db)


@app.put("/projects/{project_id}/styleguide")
def update_project_styleguide(project_id: int, payload: schemas.StyleguideSectionIn, db: Session = Depends(get_db)):
    manager = _require_admin(payload.manager_id, db)
    project = _get_project(project_id, db)
    value = _validated(payload, allow_none=True)
    project.styleguide, before = _apply_section(project.styleguide, payload.lang, payload.section, value)
    if before != value:
        db.add(models.StyleguideChange(
            client_id=project.client_id, project_id=project.id, lang=payload.lang, section=payload.section,
            value_before=before, value_after=value, changed_by_name=manager.name,
        ))
    db.commit()
    return get_project_styleguide(project_id, db)


@app.post("/styleguide/changes/{change_id}/revert")
def revert_styleguide_change(change_id: int, payload: schemas.StyleguideRevertIn, db: Session = Depends(get_db)):
    """«Вернуть»: puts the section back to its value before that change
    (logged as a new change, so the revert itself can be undone too)."""
    manager = _require_admin(payload.manager_id, db)
    ch = db.get(models.StyleguideChange, change_id)
    if ch is None:
        raise HTTPException(404, "Изменение не найдено.")
    target = ch.value_before
    if ch.project_id is not None:
        project = _get_project(ch.project_id, db)
        project.styleguide, before = _apply_section(project.styleguide, ch.lang, ch.section, target)
    else:
        if target is None:
            client = _get_client(ch.client_id, db)
            client.styleguide, before = _apply_section(client.styleguide, ch.lang, ch.section, None)
        else:
            client = _get_client(ch.client_id, db)
            client.styleguide, before = _apply_section(client.styleguide, ch.lang, ch.section, target)
    db.add(models.StyleguideChange(
        client_id=ch.client_id, project_id=ch.project_id, lang=ch.lang, section=ch.section,
        value_before=before, value_after=target, changed_by_name=f"{manager.name} (вернул)",
    ))
    db.commit()
    if ch.project_id is not None:
        return get_project_styleguide(ch.project_id, db)
    return get_client_styleguide(ch.client_id, db)


def _get_project(project_id: int, db: Session) -> models.Project:
    project = db.get(models.Project, project_id)
    if project is None:
        raise HTTPException(404, "Проект не найден.")
    return project


def _estimate_batch_minutes(db: Session, volume_chars: int) -> int | None:
    """A rough ETA for a still-processing batch job, learned from how long
    past batch jobs of a similar size actually took (Александр asked for
    some kind of estimate, even approximate, instead of only elapsed time).
    This is explicitly NOT a promise — Anthropic's queue is shared with
    every other customer and its own speed varies run to run, sometimes a
    lot, even for the exact same document — just a starting expectation so
    "processing" isn't a total blank.

    Pools total characters and total minutes across the last 20 finished
    batch jobs (rather than averaging each job's own rate) so a handful of
    small, fast jobs can't dominate the estimate the way a naive average of
    ratios would. Returns None until there's at least one finished batch
    job to learn from, or if volume_chars is 0."""
    if volume_chars <= 0:
        return None
    rows = (
        db.query(models.MultiCheck.batch_volume_chars, models.MultiCheck.created_at, models.MultiCheck.completed_at)
        .filter(
            models.MultiCheck.status == "completed",
            models.MultiCheck.batch_id != "",
            models.MultiCheck.completed_at.isnot(None),
            models.MultiCheck.batch_volume_chars > 0,
        )
        .order_by(models.MultiCheck.completed_at.desc())
        .limit(20)
        .all()
    )
    total_chars = 0.0
    total_minutes = 0.0
    for chars, created, completed in rows:
        minutes = (completed - created).total_seconds() / 60.0
        # A job clocked at under a minute is more likely measurement noise
        # (fixed overhead, a request that happened to be answered almost
        # immediately) than a real per-character rate — counting it would
        # let one lucky fast job massively overstate how fast Anthropic's
        # queue actually runs.
        if minutes < 1:
            continue
        total_chars += chars
        total_minutes += minutes
    if total_chars <= 0 or total_minutes <= 0:
        return None
    chars_per_minute = total_chars / total_minutes
    if chars_per_minute <= 0:
        return None
    return max(1, round(volume_chars / chars_per_minute))


def _catalog_languages(project_id: int, db: Session) -> list[str]:
    """Returns exactly what's stored for this project — deliberately NOT
    passed through merge_lang_codes. That helper collapses same-language
    entries gathered automatically from a document (where a bare "ko" and
    a region-qualified "ko-KR" really are just two spellings of the same
    physical column), which is the wrong behaviour here: the catalog is a
    manually-curated list the admin builds one explicit add at a time (see
    add_catalog_language below), so a bare "es" alongside "es-ar"/"es-mx"
    is a third, deliberately separate entry, not an ambiguous duplicate to
    silently drop. Merging it away made an admin's own explicit add
    invisible — found from Александр adding "es"/"fr" and seeing nothing
    change (they WERE saved, just never shown)."""
    langs = {
        row[0]
        for row in db.query(models.LanguageCatalogEntry.lang_code)
        .filter(models.LanguageCatalogEntry.project_id == project_id)
        .all()
    }
    return sorted(langs)


@app.get("/projects/{project_id}/known-languages")
def known_languages(project_id: int, db: Session = Depends(get_db)):
    """The project's manually-curated "which languages do I check here"
    catalog — the frontend's source for the target-language checkboxes.

    Deliberately NOT derived from the Tone-of-address document, and NOT
    touched by anything in an uploaded check file either — Александр
    asked for this list to change ONLY when he explicitly adds or removes
    a language (see /projects/{id}/languages below), after a mislabeled
    column ("PR", meant as Portuguese but not a real code for it) used to
    silently show up as a real target language with Peru's flag, purely
    because it happened to look language-shaped in an uploaded file.

    merge_lang_codes collapses same-language entries at different
    granularities into one (keeping the more specific spelling) while
    keeping genuinely distinct regional variants (es-ES vs es-AR vs
    es-MX) separate, since those really do mean different rules and must
    be picked explicitly."""
    _get_project(project_id, db)
    return {"languages": _catalog_languages(project_id, db)}


@app.post("/projects/{project_id}/languages")
def add_catalog_language(project_id: int, payload: schemas.LanguageIn, db: Session = Depends(get_db)):
    """Adds one language to the project's manually-curated catalog —
    admin-only, and (together with the DELETE below) the ONLY way a
    language ever enters or leaves this list. Accepts the same display
    styles as everywhere else ("ES (MX)", "es-mx") via _label_to_code —
    including anything already taught in the global /language-aliases
    dictionary. Idempotent: adding an already-present language just
    returns the current list, no error."""
    _require_admin(payload.manager_id, db)
    _get_project(project_id, db)
    code = _label_to_code(payload.lang_code.strip(), _load_alias_map(db))
    if not code or " " in code or len(code) > 12:
        raise HTTPException(400, "Некорректный код языка.")
    exists = (
        db.query(models.LanguageCatalogEntry)
        .filter(models.LanguageCatalogEntry.project_id == project_id, models.LanguageCatalogEntry.lang_code == code)
        .first()
    )
    if not exists:
        db.add(models.LanguageCatalogEntry(project_id=project_id, lang_code=code))
        db.commit()
    return {"languages": _catalog_languages(project_id, db)}


@app.delete("/projects/{project_id}/languages/{lang_code}")
def delete_catalog_language(project_id: int, lang_code: str, manager_id: int, db: Session = Depends(get_db)):
    """Removes one language from the project's catalog — admin-only.
    lang_code is matched case-insensitively (the frontend always displays
    codes upper-cased, but stores them lower-cased, same as everywhere
    else in this app)."""
    _require_admin(manager_id, db)
    _get_project(project_id, db)
    deleted = (
        db.query(models.LanguageCatalogEntry)
        .filter(
            models.LanguageCatalogEntry.project_id == project_id,
            models.LanguageCatalogEntry.lang_code == lang_code.strip().lower(),
        )
        .delete()
    )
    if not deleted:
        raise HTTPException(404, f"Язык «{lang_code}» не найден в списке языков этого проекта.")
    db.commit()
    return {"languages": _catalog_languages(project_id, db)}


# ------------------------------------------------------- language aliases ---
# GLOBAL (not per-project, unlike the catalog above) — Александр's own idea:
# any manager teaches the platform "this raw spelling means this language"
# once, here, and it's recognized everywhere from then on (a file's own
# column header, the Tone-of-address document, a manually-typed catalog
# addition) — see models.LanguageAlias and app.excel_multi._label_to_code.
# Deliberately open to every folder, not just the admin one: unlike the
# catalog (which decides what actually gets billed/checked), a wrong or
# redundant alias is low-stakes and self-correcting — everyone can see who
# added what and fix a mistake themselves, which is the point of not
# gatekeeping it.

def _alias_out(row: models.LanguageAlias) -> dict:
    return {
        "id": row.id,
        "alias": row.alias,
        "canonical_code": row.canonical_code,
        "added_by_name": row.added_by_name,
        "created_at": row.created_at,
    }


@app.get("/language-aliases")
def list_language_aliases(db: Session = Depends(get_db)):
    rows = db.query(models.LanguageAlias).order_by(models.LanguageAlias.alias).all()
    return {"aliases": [_alias_out(r) for r in rows]}


@app.post("/language-aliases")
def add_language_alias(payload: schemas.LanguageAliasIn, db: Session = Depends(get_db)):
    """Any folder may add — this is a shared dictionary, not a per-project
    setting, and mistakes here are cheap (see the section comment above).
    `alias` is stored trimmed and lower-cased; `canonical_code` goes
    through the same _label_to_code normalization as everywhere else (so
    e.g. "ES (MX)" typed as a canonical code still becomes "es-mx") —
    it's resolved WITHOUT consulting the alias table itself, so one alias
    can never silently chain into another. One alias means exactly one
    language: adding an already-present alias 409s rather than silently
    overwriting what it used to mean — delete it first if it should now
    point somewhere else."""
    manager = _get_manager(payload.manager_id, db)
    alias = payload.alias.strip().lower()
    if not alias or len(alias) > 60:
        raise HTTPException(400, "Введите вариант написания языка (до 60 символов).")
    code = _normalize_lang_label(payload.canonical_code.strip())
    if not code or " " in code or len(code) > 12:
        raise HTTPException(400, "Некорректный код языка.")
    exists = db.query(models.LanguageAlias).filter(models.LanguageAlias.alias == alias).first()
    if exists:
        raise HTTPException(
            409,
            f"«{payload.alias.strip()}» уже есть в словаре (означает «{exists.canonical_code}»). "
            "Сначала удалите его, если хотите привязать к другому языку.",
        )
    db.add(models.LanguageAlias(alias=alias, canonical_code=code, added_by_name=manager.name))
    db.commit()
    rows = db.query(models.LanguageAlias).order_by(models.LanguageAlias.alias).all()
    return {"aliases": [_alias_out(r) for r in rows]}


@app.delete("/language-aliases/{alias_id}")
def delete_language_alias(alias_id: int, manager_id: int, db: Session = Depends(get_db)):
    """Any folder may delete — see the section comment above for why this
    isn't admin-gated the way the per-project catalog is."""
    _get_manager(manager_id, db)
    row = db.get(models.LanguageAlias, alias_id)
    if row is None:
        raise HTTPException(404, "Этот вариант уже удалён или не существует.")
    db.delete(row)
    db.commit()
    rows = db.query(models.LanguageAlias).order_by(models.LanguageAlias.alias).all()
    return {"aliases": [_alias_out(r) for r in rows]}


# --------------------------------------------------------- single check ---
# Any folder may run checks — only structural changes above are admin-only.

@app.post("/check", response_model=schemas.CheckOut)
async def check(payload: schemas.CheckIn, db: Session = Depends(get_db)):
    if not payload.source.strip() or not payload.translation.strip():
        return schemas.CheckOut(findings=[])

    project = None
    if payload.project_id:
        project = _get_project(payload.project_id, db)

    findings = run_rule_checks(
        payload.source, payload.translation, payload.checks,
        lang_code=payload.target_lang,
    )
    rules = sg_mod.rules_for_code(_project_styleguide(project), payload.target_lang)
    if rules:
        findings += sg_mod.check_row(
            payload.source, payload.translation, rules, sg_mod.lang_key_for(payload.target_lang) or "",
        )
    for f in findings:
        f.setdefault("confidence", 100)  # algorithmic = certain, see excel_multi
    manager_obj = db.get(models.Manager, payload.manager_id) if payload.manager_id else None
    extra_for_ai = _with_domain_note(
        payload.extra_instructions,
        project.name if project is not None else None,
        manager_obj.name if manager_obj is not None else None,
        project_description=project.description if project is not None else None,
    )
    ai_findings, cost_usd = await run_ai_checks(
        payload.source, payload.translation, payload.checks, extra_for_ai,
        payload.target_lang, payload.source_lang, sg_mod.prompt_text(rules),
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


@app.delete("/projects/{project_id}/history/{single_check_id}")
def delete_single_check(project_id: int, single_check_id: int, manager_id: int, db: Session = Depends(get_db)):
    # Same per-folder ownership scoping as every other history endpoint —
    # a manager can only ever delete their OWN point checks.
    _get_project(project_id, db)
    record = db.get(models.SingleCheck, single_check_id)
    if record is None or record.project_id != project_id or record.manager_id != manager_id:
        raise HTTPException(404, "Проверка не найдена.")
    # Its report view (and translator links) go with it.
    for mc in db.query(models.MultiCheck).filter(models.MultiCheck.single_check_id == record.id).all():
        db.delete(mc)
    db.delete(record)
    db.commit()
    return {"ok": True}


def _single_check_as_results(sc: models.SingleCheck) -> dict:
    """A single check in the multi-check results shape: one sheet, one
    language, one row (plus the usual row-0 tone / system boxes)."""
    lang = sc.target_lang or "xx"
    findings = sc.findings or []
    tone = [f for f in findings if f.get("type") == "register_summary"]
    system = [f for f in findings if f.get("type") == "system"]
    real = [f for f in findings if f.get("type") not in ("register_summary", "system")]
    rows = []
    if real:
        rows.append({"excel_row": 1, "context": "", "source": sc.source, "translation": sc.translation, "findings": real})
    if tone:
        rows.append({"excel_row": 0, "context": "ℹ️ Тон обращения", "source": "", "translation": "", "findings": tone})
    if system:
        rows.append({"excel_row": 0, "context": "⚠ Системное предупреждение", "source": "", "translation": "", "findings": system})
    summary = {
        "sheets": 1,
        "rows_checked": 1,
        "languages_checked": [lang],
        "total_findings": len(real),
        "cost_usd": sc.cost_usd or 0.0,
        "models_by_lang": {lang: route_for_lang(lang).label},
    }
    return {
        "sheets": [{
            "sheet_name": "Точечная проверка",
            "source_lang": sc.source_lang,
            "languages_checked": [lang],
            "languages": {lang: rows},
            "unrecognized_columns": [],
        }],
        "summary": summary,
    }


@app.post("/projects/{project_id}/history/{single_check_id}/report")
def single_check_report(project_id: int, single_check_id: int, manager_id: int, db: Session = Depends(get_db)):
    """Report for a single («точечная») check (2026-10-01, Александр): made
    once as a report record, so ✓ / ? / ✕, the translator link, the QA
    head's step, 💾 — everything works exactly like for a file check.
    Returns its multi_check_id; the same one on every later call."""
    _get_project(project_id, db)
    sc = db.get(models.SingleCheck, single_check_id)
    if sc is None or sc.project_id != project_id or sc.manager_id != manager_id:
        raise HTTPException(404, "Проверка не найдена.")
    mc = db.query(models.MultiCheck).filter(models.MultiCheck.single_check_id == sc.id).first()
    if mc is None:
        results = _single_check_as_results(sc)
        when = ""
        if sc.created_at:
            created = sc.created_at if sc.created_at.tzinfo else sc.created_at.replace(tzinfo=datetime.timezone.utc)
            moscow = datetime.timezone(datetime.timedelta(hours=3))  # MSK, no DST
            when = created.astimezone(moscow).strftime("%d.%m.%Y %H:%M")
        mc = models.MultiCheck(
            project_id=project_id,
            manager_id=manager_id,
            filename=f"Точечная проверка {when}".strip(),
            source_lang=sc.source_lang or "",
            checks_run=sc.checks_run or [],
            summary=results["summary"],
            results=results,
            status="completed",
            performed_by_name=sc.performed_by_name or "",
            cost_usd=sc.cost_usd or 0.0,
            created_at=sc.created_at,
            completed_at=sc.created_at,
            single_check_id=sc.id,
        )
        db.add(mc)
        db.commit()
        db.refresh(mc)
    return {"multi_check_id": mc.id}


# ---------------------------------------------------------- multi check ---

DEFAULT_MULTI_CHECKS = [
    "numbers", "placeholders", "max_length", "register", "typo",
    "untranslatable", "completeness", "punctuation", "term_consistency",
]


@app.post("/projects/{project_id}/multi-check/detect-languages")
async def detect_file_languages(project_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Language codes found as column headers in an uploaded file, checked
    against the project's own language catalog (see known_languages
    above) — read-only: just parses the file and reports what's in it,
    doesn't run any check, store anything, or touch the catalog itself.

    Four buckets, not three:
    - languages: column headers that look like a language code AND match
      something already in the project's catalog (via the same safe
      resolve_lang_code bridging used everywhere else) — these become
      selectable/checkable target languages.
    - unknown_languages: headers that look like a language code but match
      NOTHING in the catalog — Александр hit this concretely: a column
      literally labelled "PR" (meant as an abbreviation for Portuguese,
      but not a real code for it) used to get silently treated as a real
      target language, with Peru's flag. Now it's surfaced here instead,
      so the manager can either rename the column (if it was a mistake)
      or explicitly add the language to the catalog first (if it's
      genuinely new) — never have it added FOR them.
    - unrecognized_columns: headers that don't even look like a language
      code at all (e.g. "Task name") — unrelated to the catalog, exactly
      as before.
    - duplicate_languages: a language code assigned to 2+ columns (e.g. two
      columns both headed "ru") — a real structural ambiguity, since only
      one of them can actually be used per row (see parse_workbook's
      col_letters_by_code). Caught live on Александр's real file
      (2026-09-22): a duplicated "ru" header doubled his "missing
      translation" findings for ru and silently discarded one column's
      data with no record of it anywhere. The same ambiguity is ALSO
      flagged after a check runs (see _duplicate_language_warning in
      app.excel_multi) in case it isn't noticed here first — but catching
      it here means before any AI-backed check has been paid for.

    Given back before the manager presses "start" instead of only
    surfacing inside a finished report, so any of these situations can be
    caught and fixed up front — rather than only noticed afterward, by
    which point an AI-backed check may already have been paid for without
    ever having covered the language that needed it."""
    _get_project(project_id, db)
    file_bytes = await file.read()
    try:
        sheets = parse_workbook(file_bytes, _load_alias_map(db))
    except Exception:
        raise HTTPException(400, "Не удалось прочитать файл — убедитесь, что это .xlsx с языковыми колонками.")
    langs: set[str] = set()
    unrecognized: set[str] = set()
    duplicates: dict[str, list[str]] = {}
    for s in sheets:
        langs.update(s["languages"])
        unrecognized.update(s.get("unrecognized_columns", []))
        for code, cols in (s.get("duplicate_language_columns") or {}).items():
            duplicates.setdefault(code, []).append(f"{s['sheet_name']}: {', '.join(cols)}")

    catalog = {
        row[0]
        for row in db.query(models.LanguageCatalogEntry.lang_code)
        .filter(models.LanguageCatalogEntry.project_id == project_id)
        .all()
    }
    known: set[str] = set()
    unknown: set[str] = set()
    # Same normalization as the file's headers (e.g. an older «pt» → «pt-br»).
    _amap = _load_alias_map(db)
    catalog_norm = {_label_to_code(c, _amap) for c in catalog}
    for code in langs:
        target = known if lang_matches_exactly(code, catalog_norm) else unknown
        target.add(code)

    return {
        "languages": merge_lang_codes(known),
        "unknown_languages": sorted(unknown),
        "unrecognized_columns": sorted(unrecognized),
        "duplicate_languages": duplicates,
    }


@app.post("/projects/{project_id}/multi-check/verify-languages")
async def verify_file_languages(
    project_id: int,
    file: UploadFile = File(...),
    codes: str = Form(...),
    db: Session = Depends(get_db),
):
    """Александр's redesign: rather than trusting an auto-generated "here's
    what we found" list and hoping a missing language gets NOTICED (people
    are bad at spotting an absence from a list — that's exactly how a real
    language went missing before this existed), the manager states up
    front which languages they expect to check, and this endpoint is the
    explicit yes/no per language, with the exact fix when it's no.

    codes: comma-separated canonical language codes the manager ticked
    (from the project's own catalog — known_languages / detect-languages —
    so these are already in the project's own canonical spelling, e.g.
    "es-mx" not "ES (MX)"). For each one, resolve_lang_code is tried
    against every language column actually found in THIS file — the same
    safe bridging (parenthesized/space-separated display styles, ko vs
    ko-KR granularity, country-code-style shorthand) already used
    everywhere else, so a code that's merely spelled differently in the
    file still counts as found; only a code with no safe match at all (not
    present, or genuinely ambiguous between several columns) is reported
    missing. Read-only, like detect-languages: doesn't run any check or
    store anything.

    `found` is deliberately still computed against the UNION of every
    sheet's languages (unchanged from before) — a target language is
    legitimately allowed to be missing from one sheet of a multi-sheet
    file just because that sheet's content doesn't need it (Александр's
    own call, 2026-09-17), so "present somewhere in the document" is the
    right bar for a target language.

    The SOURCE language is different: every sheet needs the original text
    to translate from, so silently missing it on even one sheet (a column
    renamed or deleted by mistake on just that sheet, while it's still
    intact elsewhere — exactly what caught Александр out testing this,
    2026-09-17: he renamed «RU» on one of two sheets, and this endpoint
    correctly reported "ru" found, since it genuinely was — just not on
    the sheet he'd edited) means that sheet's rows get silently skipped
    for that language, with zero warning. `missing_from_sheets` — the
    names of every sheet where this code did NOT resolve — lets the
    SOURCE-language confirmation on the frontend additionally require an
    empty list (found on every sheet), while the existing target-language
    confirmation keeps reading only `found` and ignoring this field, so
    its own, deliberately looser behavior is completely unchanged."""
    _get_project(project_id, db)
    alias_map = _load_alias_map(db)
    # Same dictionary + spelling normalization as the file's own headers,
    # then a strict exact match (2026-10-01, Александр — no guessing).
    requested = [c.strip() for c in codes.split(",") if c.strip()]
    if not requested:
        raise HTTPException(400, "Не выбрано ни одного языка для подтверждения.")
    file_bytes = await file.read()
    try:
        sheets = parse_workbook(file_bytes, alias_map)
    except Exception:
        raise HTTPException(400, "Не удалось прочитать файл — убедитесь, что это .xlsx с языковыми колонками.")
    file_langs: set[str] = set()
    per_sheet_langs: list[tuple[str, set[str]]] = []
    for s in sheets:
        file_langs.update(s["languages"])
        per_sheet_langs.append((s["sheet_name"], set(s["languages"])))
    results = [
        {
            "code": code,
            "found": lang_matches_exactly(_label_to_code(code, alias_map), file_langs),
            "missing_from_sheets": [
                name for name, langs in per_sheet_langs
                if not lang_matches_exactly(_label_to_code(code, alias_map), langs)
            ],
        }
        for code in requested
    ]
    return {"results": results}


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
    # "Срочно" checkbox — forces the live/synchronous path even for a job
    # that would otherwise go to Anthropic's cheaper batch queue, so the
    # manager gets a result in the same request instead of waiting up to
    # an hour. Costs 2x (the batch queue is exactly half price — see
    # claude_client.BATCH_PRICE_DISCOUNT — so skipping it is full price).
    urgent: bool = Form(False),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
):
    # Captured up front (rather than relying on created_at's own
    # default-at-insert-time, which for the live path below would land AFTER
    # all the AI checking already ran) so a completed record's created_at
    # genuinely marks when the request came in — needed to show a real,
    # accurate "проверка заняла N минут" for a live/synchronous check, not
    # just for a batch one. See the duration_minutes plumbing below.
    started_at = datetime.datetime.now(datetime.timezone.utc)

    project_obj = _get_project(project_id, db)
    manager_obj = _get_manager(manager_id, db)
    extra_instructions = _with_domain_note(
        extra_instructions, project_obj.name, manager_obj.name, project_description=project_obj.description,
    )
    file_bytes = await file.read()

    try:
        sheets = parse_workbook(file_bytes, _load_alias_map(db))
    except Exception:
        raise HTTPException(400, "Не удалось прочитать файл — убедитесь, что это .xlsx с языковыми колонками.")

    if not sheets:
        raise HTTPException(400, "В файле не найдено ни одной колонки с кодом языка.")

    selected_checks = [c.strip() for c in checks.split(",") if c.strip()] or DEFAULT_MULTI_CHECKS

    # Ticked catalog codes go through the SAME dictionary + spelling
    # normalization as the file's headers (and as «Подтвердить выбор
    # языков»), so e.g. an older catalog entry «pt» meets the file's
    # «pt-BR» column («pt» → «pt-br»). Before this fix the confirmation said
    # «найден», but the check itself silently skipped the language.
    alias_map = _load_alias_map(db)
    target_filter = {
        _label_to_code(c.strip(), alias_map).lower() for c in target_langs.split(",") if c.strip()
    } or None
    resolved_source = pick_source_lang(sheets, source_lang.strip().lower() or None)
    if target_filter is not None:
        file_targets = {l.lower() for s_ in sheets for l in s_["languages"] if l != resolved_source}
        if not (target_filter & file_targets):
            raise HTTPException(
                400,
                "Ни один из выбранных языков не найден в файле — проверка не запущена. "
                "Нажмите «Подтвердить выбор языков» и при необходимости добавьте написание в «Словарь языков».",
            )

    # 2026-09-29 redesign: every upload runs through the live background path.
    # Languages are now routed to different vendors (Claude or GPT, see
    # claude_client.LANG_MODEL_TIER), and Anthropic's Message Batches queue
    # can't run GPT-routed languages — so the old "large non-urgent upload →
    # Anthropic batch" branch is retired for NEW checks ("Срочно" no longer
    # changes anything). Batch jobs already in flight still finish through
    # multi_check_detail as before.
    if True:
        # The actual AI check now runs in the background too, instead of
        # being awaited right here — added 2026-09-26, after Александр
        # reported that "Failed to fetch" kept happening even after the
        # second-opinion pass below was already backgrounded. Root cause:
        # this branch used to `await run_multi_check(...)` directly inside
        # the request handler, so a medium/large document — "Срочно" or
        # not — could block the response long enough for the browser's own
        # fetch to give up, even though the check always finished and saved
        # fine a while later (that's why it then showed up correctly in
        # history — nothing was ever actually lost, just not delivered back
        # to that request). The record now starts as "processing" (no
        # batch_id — this is still the live/full-price path, nothing was
        # handed to Anthropic's batch queue) and _run_live_check_background
        # takes it from there; the frontend polls multi-check detail the
        # same way it already does for a real batch job, distinguished only
        # by the "batch": false flag (see CheckRunner.tsx, which uses it to
        # pick a much faster poll interval here).
        record = models.MultiCheck(
            project_id=project_id,
            filename=file.filename or "upload.xlsx",
            source_lang=resolved_source,
            checks_run=selected_checks,
            summary={},
            results={},
            status="processing",
            performed_by_name=manager_name.strip(),
            manager_id=manager_id,
            cost_usd=0.0,
            created_at=started_at,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        background_tasks.add_task(
            _run_live_check_background,
            record.id, sheets, resolved_source, selected_checks, extra_instructions, target_filter,
            _project_styleguide(project_obj),
        )
        return {
            "multi_check_id": record.id,
            "status": "processing",
            "filename": record.filename,
            "source_lang": resolved_source,
            # Never a real Anthropic batch job — see the comment above and
            # CheckRunner.tsx, which uses this to skip the batch-only
            # "обычно занимает до часа" messaging and use a fast poll.
            "batch": False,
            "progress": None,
            "created_at": record.created_at.isoformat(),
            "estimated_minutes": None,
            "checks_run": record.checks_run,
        }


@app.get(
    "/projects/{project_id}/multi-check",
    response_model=list[schemas.MultiCheckHistoryOut],
)
async def multi_check_history(
    project_id: int, manager_id: int,
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
):
    """Scoped to the requesting folder only — same per-folder history
    scoping as single_check_history, above."""
    _get_project(project_id, db)
    records = db.query(models.MultiCheck).filter(
        models.MultiCheck.project_id == project_id,
        models.MultiCheck.manager_id == manager_id,
        models.MultiCheck.single_check_id.is_(None),
    ).order_by(models.MultiCheck.created_at.desc()).limit(50).all()

    out = []
    for r in records:
        progress = None
        if r.status == "processing" and r.batch_id:
            # One Anthropic call per still-processing upload — in practice
            # there's rarely more than one at a time — so the history list
            # can show a real "готово X из Y" without the manager having to
            # reopen that specific check's page to trigger a poll.
            finalized, progress = await try_finalize_batch(r.batch_id, r.results["skeleton"])
            if finalized is not None:
                # Same deferral as the live multi-check path above — the
                # Sonnet-only second opinion runs in the background instead
                # of making THIS poll (already loaded with everyone else's
                # history) wait on one more API call per language.
                finalized["second_opinion_pending"] = True
                r.results = finalized
                r.summary = finalized["summary"]
                r.status = "completed"
                r.cost_usd = finalized["summary"].get("cost_usd", 0.0)
                # Records how long this batch job actually took (together
                # with created_at and batch_volume_chars) — future estimates
                # learn from this. See _estimate_batch_minutes.
                r.completed_at = datetime.datetime.now(datetime.timezone.utc)
                db.commit()
                db.refresh(r)
                background_tasks.add_task(_run_second_opinion_background, r.id, finalized)
                progress = None
        out.append(schemas.MultiCheckHistoryOut(
            id=r.id, filename=r.filename, source_lang=r.source_lang,
            summary=r.summary, status=r.status, performed_by_name=r.performed_by_name,
            created_at=r.created_at.isoformat(),
            cost_usd=r.cost_usd,
            progress=progress,
            estimated_minutes=_estimate_batch_minutes(db, r.batch_volume_chars) if r.status == "processing" else None,
            completed_at=r.completed_at.isoformat() if r.completed_at else None,
            second_opinion_pending=bool((r.results or {}).get("second_opinion_pending", False)),
        ))
    return out


@app.get("/projects/{project_id}/multi-check/{multi_check_id}")
async def multi_check_detail(
    project_id: int, multi_check_id: int, manager_id: int,
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
):
    _get_project(project_id, db)
    record = db.get(models.MultiCheck, multi_check_id)
    if record is None or record.project_id != project_id or record.manager_id != manager_id:
        raise HTTPException(404, "Проверка не найдена.")

    progress = None
    if record.status == "processing" and record.batch_id:
        finalized, progress = await try_finalize_batch(record.batch_id, record.results["skeleton"])
        if finalized is not None:
            # Same deferral as everywhere else — see
            # _run_second_opinion_background's own docstring.
            finalized["second_opinion_pending"] = True
            record.results = finalized
            record.summary = finalized["summary"]
            record.status = "completed"
            record.cost_usd = finalized["summary"].get("cost_usd", 0.0)
            # See multi_check_history's matching line — records how long
            # this batch job actually took, for future estimates.
            record.completed_at = datetime.datetime.now(datetime.timezone.utc)
            db.commit()
            db.refresh(record)
            background_tasks.add_task(_run_second_opinion_background, record.id, finalized)

    if record.status == "processing":
        return {
            "multi_check_id": record.id,
            "status": "processing",
            "filename": record.filename,
            "source_lang": record.source_lang,
            # Whether this is a real Anthropic batch job (has a batch_id) or
            # a live/"Срочно" check now running in the background (see
            # _run_live_check_background) — added 2026-09-26. The two look
            # the same status-wise but need different messaging/polling on
            # the frontend: a batch can genuinely take up to an hour and is
            # polite about how often it checks Anthropic's own batch status,
            # while a live check has no external queue to be polite to and
            # is normally done in well under a minute, so CheckRunner.tsx
            # uses this flag to poll it much faster and skip the "обычно
            # занимает до часа" wording.
            "batch": bool(record.batch_id),
            # Real counts from Anthropic (how many of the batch's requests
            # are done), not a guessed time estimate — see excel_multi._batch_progress.
            # Always None for a live check (no batch to report counts for).
            "progress": progress,
            # Lets the UI show elapsed waiting time as a fallback while the
            # counts above are still flat — see the submission response.
            "created_at": record.created_at.isoformat(),
            # Same rough, non-binding ETA as the submission response — kept
            # live here too so it reflects the freshest historical data on
            # every poll, not just what was known at submission time. Always
            # None for a live check (_estimate_batch_minutes returns None
            # for batch_volume_chars <= 0, which a live check's record
            # always has — it was never sized for the batch queue).
            "estimated_minutes": _estimate_batch_minutes(db, record.batch_volume_chars),
        }

    if record.status == "failed":
        # Only reachable for a live/"Срочно" check that raised inside
        # _run_live_check_background (a genuine batch failure instead
        # surfaces as an errored/expired result_type per language, handled
        # inside finalize_batch_results — it never marks the whole record
        # "failed"). Added 2026-09-26 alongside backgrounding the live path,
        # so a background exception doesn't leave the record stuck on
        # "processing" forever with no explanation — see CheckRunner.tsx's
        # matching render block.
        return {
            "multi_check_id": record.id,
            "status": "failed",
            "filename": record.filename,
            "source_lang": record.source_lang,
            "error": (record.results or {}).get("error", "Не удалось выполнить проверку."),
            "created_at": record.created_at.isoformat(),
        }

    return {
        "multi_check_id": record.id,
        "status": "completed",
        "filename": record.filename,
        "source_lang": record.source_lang,
        "summary": record.summary,
        "sheets": record.results.get("sheets", []),
        "cost_usd": record.cost_usd,
        # Александр asked for the check's report to show how long it took —
        # the frontend computes the duration from these two timestamps, the
        # same way it already computes elapsed time for a still-processing
        # check. completed_at is absent (None) only for a record that
        # finished before this was tracked.
        "created_at": record.created_at.isoformat(),
        "completed_at": record.completed_at.isoformat() if record.completed_at else None,
        "checks_run": record.checks_run,
        # True until the background Sonnet-only second-opinion pass finishes
        # (see _run_second_opinion_background) — the frontend polls this
        # endpoint while it's true, same as it does for status=="processing".
        "second_opinion_pending": bool((record.results or {}).get("second_opinion_pending", False)),
        # Manager's per-finding review for the translators' table (2026-09-29).
        "review": record.review or {},
        # Active translator links, {lang: token} (see models.ShareLink).
        "shares": {sl.lang: sl.token for sl in record.share_links if not sl.revoked},
        # Translators' answers from their share-link pages (2026-09-30).
        "translator_review": record.translator_review or {},
        # Blocks of this report already in «Сохранённое» ("<sheet>|<lang>|<row>").
        "saved_keys": [
            sc.source_key.split("|", 1)[1]
            for sc in db.query(models.SavedCase).filter(
                models.SavedCase.multi_check_id == record.id, models.SavedCase.manager_id == record.manager_id,
            ).all()
        ],
    }


@app.put("/projects/{project_id}/multi-check/{multi_check_id}/review")
def update_multi_check_review(
    project_id: int, multi_check_id: int, payload: schemas.MultiCheckReviewIn, db: Session = Depends(get_db),
):
    """Saves one finding's decision ("Включить" / "Отклонить"), its Crowdin
    link(s) and the manager's note — or, for key "note|<lang>", that
    language's general note. 2026-09-29, Александр. Same folder scoping as
    multi_check_detail."""
    _get_project(project_id, db)
    record = db.get(models.MultiCheck, multi_check_id)
    if record is None or record.project_id != project_id or record.manager_id != payload.manager_id:
        raise HTTPException(404, "Проверка не найдена.")
    key = payload.key.strip()
    if not key or len(key) > 300:
        raise HTTPException(400, "Некорректный ключ замечания.")
    if payload.decision not in (None, "accept", "question", "reject"):
        raise HTTPException(400, "Некорректное решение.")
    links = (payload.links or "").strip()[:4000]
    note = (payload.note or "").strip()[:4000]
    review = dict(record.review or {})
    # Keep what the head of QA did on the share page ("sent", "okk_comment").
    entry = dict(review.get(key) or {})
    entry.update({"decision": payload.decision, "links": links, "note": note})
    if payload.decision is None and not links and not note and not entry.get("sent") and not entry.get("okk_comment"):
        review.pop(key, None)
    else:
        review[key] = entry
    record.review = review  # reassign so SQLAlchemy notices the JSON change
    db.commit()
    return {"ok": True, "review": review}


def _own_multi_check(project_id: int, multi_check_id: int, manager_id: int, db: Session) -> models.MultiCheck:
    _get_project(project_id, db)
    record = db.get(models.MultiCheck, multi_check_id)
    if record is None or record.project_id != project_id or record.manager_id != manager_id:
        raise HTTPException(404, "Проверка не найдена.")
    return record


@app.post("/projects/{project_id}/multi-check/{multi_check_id}/share")
def create_share_link(project_id: int, multi_check_id: int, payload: schemas.ShareLinkIn, db: Session = Depends(get_db)):
    """Returns this language's active translator link, creating it if there
    isn't one yet (2026-09-29, Александр). Never expires; see revoke below."""
    record = _own_multi_check(project_id, multi_check_id, payload.manager_id, db)
    lang = payload.lang.strip()
    if not lang or len(lang) > 40:
        raise HTTPException(400, "Не указан язык.")
    langs = {l for sh in (record.results or {}).get("sheets", []) for l in sh.get("languages_checked", [])}
    if lang != ALL_LANGS and lang not in langs:
        raise HTTPException(400, "Такого языка нет в этом отчёте.")
    existing = next((sl for sl in record.share_links if sl.lang == lang and not sl.revoked), None)
    if existing is None:
        # Every ✓ / ? finding must carry a Crowdin link first (2026-10-01).
        review = record.review or {}
        missing = 0
        for l in (report_langs(record.results or {}) if lang == ALL_LANGS else [lang]):
            for _, key, row, _f in numbered_findings(l, record.results or {}):
                e = review.get(key) or {}
                if row is not None and e.get("decision") in ("accept", "question") and not (e.get("links") or "").strip():
                    missing += 1
        if missing:
            raise HTTPException(
                400, f"Отчёт нельзя сгенерировать: у {missing} {'замечания' if missing % 10 == 1 and missing % 100 != 11 else 'замечаний'} "
                "с ✓ или ? нет ссылки на Crowdin.",
            )
        existing = models.ShareLink(token=secrets.token_urlsafe(24), multi_check_id=record.id, lang=lang)
        db.add(existing)
        db.commit()
        db.refresh(existing)
    return {"lang": lang, "token": existing.token, "path": f"/share/{existing.token}"}


@app.post("/projects/{project_id}/multi-check/{multi_check_id}/share/revoke")
def revoke_share_link(project_id: int, multi_check_id: int, payload: schemas.ShareLinkIn, db: Session = Depends(get_db)):
    """Turns off this language's translator link — it stops opening at once.
    A new link can be created afterwards (it gets a new address)."""
    record = _own_multi_check(project_id, multi_check_id, payload.manager_id, db)
    lang = payload.lang.strip()
    for sl in record.share_links:
        if sl.lang == lang and not sl.revoked:
            sl.revoked = True
    # Deleting the link resets this language's translator reactions and the QA
    # head's «Оставить переводчику»/«Убрать», so a new link starts the QA-head
    # stage over (Александр, 2026-10-01). The manager's ✓/?/✕, links, notes
    # and the QA head's comments stay (they're prefilled next time).
    # The all-languages link ("*") resets every language.
    of_lang = lambda k: lang == ALL_LANGS or (k.split("|") + ["", ""])[1] == lang
    record.translator_review = {k: v for k, v in (record.translator_review or {}).items() if not of_lang(k)}
    review = {}
    for k, v in (record.review or {}).items():
        if of_lang(k) and isinstance(v, dict):
            v = {kk: vv for kk, vv in v.items() if kk not in ("sent", "okk_removed")}
        review[k] = v
    record.review = review
    db.commit()
    return {"ok": True}


@app.get("/share/{token}", response_class=HTMLResponse, include_in_schema=False)
def shared_report_page(token: str, db: Session = Depends(get_db)):
    """The public, read-only translator page — see app.share_page. Only the
    random token identifies it; nothing else about the folder/project/report
    is reachable from here."""
    link = None
    if 10 <= len(token) <= 64:
        link = db.query(models.ShareLink).filter(models.ShareLink.token == token).first()
    if link is None or link.revoked or link.multi_check is None or link.multi_check.status != "completed":
        return HTMLResponse(render_not_found(), status_code=404, headers=share_page_headers(""))
    mc = link.multi_check
    nonce = secrets.token_urlsafe(16)
    page = render_shared_report(
        mc.filename, link.lang, mc.results or {}, mc.review or {}, mc.translator_review or {}, nonce,
    )
    return HTMLResponse(page, headers=share_page_headers(nonce))


def _active_share_link(token: str, db: Session) -> models.ShareLink:
    link = None
    if 10 <= len(token) <= 64:
        link = db.query(models.ShareLink).filter(models.ShareLink.token == token).first()
    if link is None or link.revoked or link.multi_check is None:
        raise HTTPException(404, "Ссылка недействительна.")
    return link


def _key_lang(link: models.ShareLink, key: str) -> str:
    """Which language a finding key on a share page belongs to — the link's
    own language, or, for an all-languages link, the one inside the key
    ("<sheet>|<lang>|<row>|<n>" / "tone|<lang>")."""
    if link.lang != ALL_LANGS:
        return link.lang
    lang = (key.split("|") + ["", ""])[1]
    if lang not in report_langs(link.multi_check.results or {}):
        raise HTTPException(400, "Такого замечания нет.")
    return lang


@app.post("/share/{token}/okk", include_in_schema=False)
def shared_report_okk(token: str, payload: schemas.ShareOkkIn, db: Session = Depends(get_db)):
    """The head of QA's step on the share page (2026-10-01, Александр): every
    finding the manager marked ✓ or ? first waits here with «Оставить
    переводчику» / «Убрать». action None only saves the «Комментарий для
    переводчика»; "keep" sends the finding on to the translator (the
    manager's note is then hidden from the page); "remove" drops it."""
    link = _active_share_link(token, db)
    mc = link.multi_check
    key = payload.key.strip()
    klang = _key_lang(link, key)
    if key not in pending_keys(klang, mc.results or {}, mc.review or {}):
        raise HTTPException(400, "Это замечание уже решено.")
    if payload.action not in (None, "keep", "remove"):
        raise HTTPException(400, "Некорректное действие.")
    review = dict(mc.review or {})
    entry = dict(review.get(key) or {})
    entry["okk_comment"] = (payload.comment or "").strip()[:4000]
    if payload.links is not None:
        entry["links"] = payload.links.strip()[:4000]
    # The QA head's choice is stored apart from the manager's ✓/?/✕, so a new
    # link can start the QA-head stage over from the manager's marks.
    if payload.action == "keep":
        entry["sent"] = True
    elif payload.action == "remove":
        entry["okk_removed"] = True
    review[key] = entry
    mc.review = review  # reassign so SQLAlchemy notices the JSON change
    db.commit()
    return {"ok": True}


@app.post("/share/{token}/save-case", include_in_schema=False)
def shared_report_save_case(token: str, payload: schemas.ShareSaveIn, db: Session = Depends(get_db)):
    """💾 on the share page (2026-10-01, Александр): saves one finding with
    its source and translation into «Сохранённое» of the folder named here.
    The page has no login of its own, so the folder's password is required."""
    link = _active_share_link(token, db)
    mc = link.multi_check
    wanted = payload.folder.strip().lower()
    manager = next((m for m in db.query(models.Manager).all() if m.name.strip().lower() == wanted), None)
    if manager is None or not verify_code(payload.code.strip(), manager.code_hash):
        raise HTTPException(401, "Неверная папка или пароль.")
    key = payload.key.strip()
    klang = _key_lang(link, key)
    visible = pending_keys(klang, mc.results or {}, mc.review or {}) | sent_keys(klang, mc.results or {}, mc.review or {})
    found = next(
        ((row, f) for _, k, row, f in numbered_findings(klang, mc.results or {}) if k == key and row is not None),
        None,
    )
    if key not in visible or found is None:
        raise HTTPException(400, "Такого замечания нет.")
    row, f = found
    sheet_idx = key.split("|")[0]
    source_key = f"{mc.id}|{sheet_idx}|{klang}|{row.get('excel_row')}|{key.split('|')[-1]}|m{manager.id}"
    existing = db.query(models.SavedCase).filter(models.SavedCase.source_key == source_key).first()
    if existing is None:
        project = db.get(models.Project, mc.project_id)
        db.add(models.SavedCase(
            source_key=source_key,
            multi_check_id=mc.id,
            manager_id=manager.id,
            project_name=project.name if project else "",
            filename=mc.filename or "",
            lang=klang,
            excel_row=row.get("excel_row") or 0,
            context=row.get("context") or "",
            source=row.get("source") or "",
            translation=row.get("translation") or "",
            findings=[{"type": f.get("type"), "severity": f.get("severity"), "message": f.get("message")}],
            saved_by_name=manager.name,
        ))
        db.commit()
    return {"ok": True, "folder": manager.name}


@app.post("/share/{token}/checked", include_in_schema=False)
def shared_report_checked(token: str, payload: schemas.ShareCheckedIn, db: Session = Depends(get_db)):
    """The manager's tick «правка проверена» next to a translator's answer."""
    link = _active_share_link(token, db)
    mc = link.multi_check
    key = payload.key.strip()
    klang = _key_lang(link, key)
    tr = dict(mc.translator_review or {})
    if key not in sent_keys(klang, mc.results or {}, mc.review or {}) or not (tr.get(key) or {}).get("decision"):
        raise HTTPException(400, "Сначала нужен ответ переводчика.")
    entry = dict(tr[key])
    entry["checked"] = bool(payload.checked)
    tr[key] = entry
    mc.translator_review = tr
    db.commit()
    return {"ok": True}


@app.post("/share/{token}/respond", include_in_schema=False)
def shared_report_respond(token: str, payload: schemas.TranslatorResponseIn, db: Session = Depends(get_db)):
    """A translator's «Принять»/«Отклонить» and comment on one finding
    (2026-09-30, Александр). Only findings the manager accepted for this
    link's language can be answered."""
    link = _active_share_link(token, db)
    mc = link.multi_check
    if payload.decision not in (None, "done", "na"):
        raise HTTPException(400, "Некорректное решение.")
    key = payload.key.strip()
    klang = _key_lang(link, key)
    if key not in sent_keys(klang, mc.results or {}, mc.review or {}):
        raise HTTPException(400, "Такого замечания нет.")
    comment = (payload.comment or "").strip()[:4000]
    tr = dict(mc.translator_review or {})
    prev = tr.get(key) or {}
    if payload.decision is None and not comment:
        tr.pop(key, None)
    else:
        tr[key] = {"decision": payload.decision, "comment": comment, "checked": bool(prev.get("checked")) and bool(payload.decision)}
    mc.translator_review = tr  # reassign so SQLAlchemy notices the JSON change
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------ «Сохранённое» ---
# Interesting cases saved from reports with the 💾 button (2026-10-01,
# Александр). Each folder sees and deletes only its own.

def _saved_case_out(sc: models.SavedCase) -> dict:
    return {
        "id": sc.id,
        "project_name": sc.project_name,
        "filename": sc.filename,
        "lang": sc.lang,
        "excel_row": sc.excel_row,
        "context": sc.context,
        "source": sc.source,
        "translation": sc.translation,
        "findings": sc.findings or [],
        "saved_by_name": sc.saved_by_name,
        "created_at": sc.created_at.isoformat() if sc.created_at else None,
    }


@app.get("/saved-cases")
def list_saved_cases(manager_id: int, db: Session = Depends(get_db)):
    _get_manager(manager_id, db)
    rows = (
        db.query(models.SavedCase)
        .filter(models.SavedCase.manager_id == manager_id)
        .order_by(models.SavedCase.created_at.desc(), models.SavedCase.id.desc())
        .all()
    )
    return {"cases": [_saved_case_out(r) for r in rows]}


@app.post("/projects/{project_id}/multi-check/{multi_check_id}/save-case")
def save_case(project_id: int, multi_check_id: int, payload: schemas.SaveCaseIn, db: Session = Depends(get_db)):
    """Copies one block (one row of one language) of a report into
    «Сохранённое». Saving the same block again just returns it."""
    record = _own_multi_check(project_id, multi_check_id, payload.manager_id, db)
    manager = _get_manager(payload.manager_id, db)
    sheets = (record.results or {}).get("sheets", [])
    if not (0 <= payload.sheet_idx < len(sheets)):
        raise HTTPException(400, "Блок не найден.")
    rows = (sheets[payload.sheet_idx].get("languages") or {}).get(payload.lang) or []
    row = next((r for r in rows if r.get("excel_row") == payload.excel_row and payload.excel_row != 0), None)
    if row is None:
        raise HTTPException(400, "Блок не найден.")
    source_key = f"{record.id}|{payload.sheet_idx}|{payload.lang}|{payload.excel_row}"
    existing = db.query(models.SavedCase).filter(models.SavedCase.source_key == source_key).first()
    if existing is None:
        project = db.get(models.Project, project_id)
        existing = models.SavedCase(
            source_key=source_key,
            multi_check_id=record.id,
            manager_id=manager.id,
            project_name=project.name if project else "",
            filename=record.filename or "",
            lang=payload.lang,
            excel_row=payload.excel_row,
            context=row.get("context") or "",
            source=row.get("source") or "",
            translation=row.get("translation") or "",
            findings=[
                {"type": f.get("type"), "severity": f.get("severity"), "message": f.get("message")}
                for f in row.get("findings") or []
            ],
            saved_by_name=manager.name,
        )
        db.add(existing)
        db.commit()
        db.refresh(existing)
    return _saved_case_out(existing)


@app.delete("/saved-cases/{case_id}")
def delete_saved_case(case_id: int, manager_id: int, db: Session = Depends(get_db)):
    _get_manager(manager_id, db)
    row = db.get(models.SavedCase, case_id)
    if row is None or row.manager_id != manager_id:
        raise HTTPException(404, "Этот кейс уже удалён.")
    db.delete(row)
    db.commit()
    return {"ok": True}


@app.get("/projects/{project_id}/multi-check/{multi_check_id}/report.xlsx")
def multi_check_report(project_id: int, multi_check_id: int, manager_id: int, db: Session = Depends(get_db)):
    _get_project(project_id, db)
    record = db.get(models.MultiCheck, multi_check_id)
    if record is None or record.project_id != project_id or record.manager_id != manager_id:
        raise HTTPException(404, "Проверка не найдена.")
    if record.status != "completed":
        raise HTTPException(409, "Проверка ещё обрабатывается — отчёт будет доступен после завершения.")

    duration_minutes = None
    if record.completed_at is not None and record.created_at is not None:
        duration_minutes = (record.completed_at - record.created_at).total_seconds() / 60

    report_bytes = build_report_workbook(
        record.filename, record.source_lang, record.results, duration_minutes=duration_minutes
    )
    filename = f"qa-report-{record.id}.xlsx"
    return StreamingResponse(
        iter([report_bytes]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/projects/{project_id}/multi-check/{multi_check_id}")
async def delete_multi_check(project_id: int, multi_check_id: int, manager_id: int, db: Session = Depends(get_db)):
    # Scoped exactly like every other multi-check lookup: a manager can only
    # ever see/act on their OWN uploads (not every folder's) — same rule as
    # multi_check_detail and multi_check_report above.
    _get_project(project_id, db)
    record = db.get(models.MultiCheck, multi_check_id)
    if record is None or record.project_id != project_id or record.manager_id != manager_id:
        raise HTTPException(404, "Проверка не найдена.")
    # Deleting a still-processing upload doubles as "cancel" — Александр
    # asked for this (a check turning out slower than expected, or just
    # changing his mind). Tell Anthropic to stop before dropping our own
    # record, so a cancelled check doesn't keep quietly racking up cost in
    # the background after the manager thinks it's gone.
    if record.status == "processing" and record.batch_id:
        await cancel_multi_check_batch(record.batch_id)
    db.delete(record)
    db.commit()
    return {"ok": True}


@app.post("/debug/model-comparison")
async def debug_model_comparison(payload: schemas.ModelComparisonIn):
    # Standalone diagnostic tool, NOT part of the product managers use —
    # see app.model_comparison's own comment for the full story
    # (Александр's ask, 2026-09-22, to settle the "does asking a cheaper
    # model several times make up for it being weaker" question with real
    # numbers instead of more reasoning on paper). Deliberately has no
    # project/manager plumbing — meant to be triggered by hand a handful of
    # times via this backend's own interactive /docs page, not called from
    # the frontend. Each call makes real, real-money calls to Anthropic
    # (up to 3 models × runs_per_model each) — total_cost_usd in the
    # response says exactly how much this one call spent.
    result = await run_model_comparison(
        context=payload.context,
        source=payload.source,
        translation=payload.translation,
        target_lang=payload.target_lang,
        source_lang=payload.source_lang,
        checks=payload.checks,
        runs_per_model=payload.runs_per_model,
        bare=payload.bare,
        two_step=payload.two_step,
        models=payload.models,
    )
    if not result:
        raise HTTPException(
            503,
            "ANTHROPIC_API_KEY не настроен на этом сервере — сравнение моделей требует реального обращения к "
            "Anthropic, тестовый режим тут не поможет.",
        )
    return result


@app.post("/debug/chunk-size-comparison")
async def debug_chunk_size_comparison(payload: schemas.ChunkSizeComparisonIn):
    # Standalone diagnostic tool, same spirit as /debug/model-comparison
    # above (no project/manager plumbing, meant to be triggered by hand via
    # this backend's own interactive /docs page) — see
    # app.model_comparison.run_chunk_size_comparison's own comment for what
    # this tests: checking each row alone (chunk size 1, today's real
    # behavior for hard languages) vs. checking the same rows together in
    # one prompt (chunk size 15, today's real behavior otherwise), on the
    # same model — Александр's ask, 2026-09-26, to find out whether the
    # current cost premium for hard languages is actually buying anything.
    # Each call makes real, real-money calls to Anthropic (one per row per
    # run for "individual" mode, plus one per run for "batched" mode) —
    # both cost totals in the response say exactly how much this one call
    # spent. Optional payload.grouped_chunk_size (2026-09-28, Александр's
    # ask) adds a THIRD mode — chunks of that size, e.g. 5 — so a middle
    # ground between the two extremes can be measured on real rows instead
    # of guessed at, before deciding whether it's worth trading some of the
    # cost premium away without losing the accuracy chunk=1 is proven to
    # buy on at least one real example (see MAX_ROWS_PER_AI_CALL_HARD's
    # own comment).
    result = await run_chunk_size_comparison(
        rows=[r.model_dump() for r in payload.rows],
        target_lang=payload.target_lang,
        source_lang=payload.source_lang,
        checks=payload.checks,
        runs_per_mode=payload.runs_per_mode,
        model=payload.model,
        grouped_chunk_size=payload.grouped_chunk_size,
    )
    if not result:
        raise HTTPException(
            503,
            "ANTHROPIC_API_KEY не настроен на этом сервере — сравнение по размеру пачки требует реального "
            "обращения к Anthropic, тестовый режим тут не поможет.",
        )
    return result
