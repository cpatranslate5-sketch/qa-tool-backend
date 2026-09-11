import json
import re

import httpx

from app.config import settings

CHECK_PROMPT = """Ты — модуль контроля качества перевода для бюро переводов. Тебе даны исходный текст и его перевод.
Проверь ТОЛЬКО те критерии, что перечислены ниже в "Что проверять" — не выходи за их рамки.

Исходный текст:
\"\"\"{source}\"\"\"

Перевод:
\"\"\"{translation}\"\"\"

Глоссарий (обязательные соответствия терминов, если есть):
{glossary}

Что проверять: {checks_description}

Верни ТОЛЬКО валидный JSON-массив найденных проблем, без markdown-обрамления и пояснений, строго в этой форме
(пустой массив [], если проблем не найдено):
[
  {{"type": "glossary|register|typo", "severity": "low|medium|high", "message": "конкретное описание проблемы на русском, с указанием места в тексте, если это осмысленно"}}
]"""

CHECK_LABELS = {
    "glossary": "соответствие глоссарию — термины должны переводиться ровно так, как указано в глоссарии",
    "register": "регистр обращения (ты/вы и эквиваленты в целевом языке) — должен быть единообразным по всему тексту",
    "typo": "опечатки, пропущенные или искажённые по смыслу фрагменты — сравни перевод с исходником построчно на предмет потери или искажения смысла",
}


async def run_ai_checks(source: str, translation: str, glossary: str, checks: list[str]) -> list[dict]:
    ai_checks = [c for c in checks if c in CHECK_LABELS]
    if not ai_checks:
        return []

    checks_description = "; ".join(CHECK_LABELS[c] for c in ai_checks)
    prompt = CHECK_PROMPT.format(
        source=source,
        translation=translation,
        glossary=glossary.strip() or "не указан",
        checks_description=checks_description,
    )

    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": settings.CLAUDE_MODEL,
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        data = resp.json()

    text_block = next((b["text"] for b in data.get("content", []) if b.get("type") == "text"), None)
    if not text_block:
        return []

    cleaned = re.sub(r"```json|```", "", text_block).strip()
    try:
        result = json.loads(cleaned)
        if isinstance(result, list):
            return result
    except Exception:
        pass
    return []
