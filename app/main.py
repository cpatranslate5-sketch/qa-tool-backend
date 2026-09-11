from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.config import settings
from app.rule_checks import run_rule_checks
from app.claude_client import run_ai_checks

app = FastAPI(title="Translation QA Tool", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"ok": True}


class CheckIn(BaseModel):
    source: str
    translation: str
    glossary: str = ""
    checks: list[str] = ["numbers", "placeholders", "glossary", "register", "typo"]


@app.post("/check")
async def check(payload: CheckIn):
    if not payload.source.strip() or not payload.translation.strip():
        return {"findings": []}

    findings = run_rule_checks(payload.source, payload.translation, payload.checks)
    ai_findings = await run_ai_checks(payload.source, payload.translation, payload.glossary, payload.checks)
    findings += ai_findings

    return {"findings": findings}
