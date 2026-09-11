import json
import re

import httpx

from app.config import settings

CHECK_LABELS = {
    "glossary": "соответствие глоссарию — термины должны переводиться ровно так, как указано в глоссарии",
    "register": "регистр обращения (ты/вы и эквиваленты в целевом языке) — должен быть единообразным по всему тексту",
    "typo": "опечатки, пропущенные или искажённые по смыслу фрагменты — сравни перевод с исходником построчно на предмет потери или искажения смысла",
}

SINGLE_PROMPT = """Ты — модуль контроля качества перевода для бюро переводов. Тебе даны исходный текст и его перевод.
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

BATCH_PROMPT = """Ты — модуль контроля качества перевода для бюро переводов. Тебе даны несколько пар (контекст, исходный текст, перевод) на один и тот же целевой язык.
Проверь ТОЛЬКО те критерии, что перечислены ниже в "Что проверять" — не выходи за их рамки.
Проверяй каждую пару независимо от остальных.

Глоссарий (обязательные соответствия терминов, если есть, действует для всех пар):
{glossary}

Что проверять: {checks_description}

Пары для проверки:
{pairs_block}

Верни ТОЛЬКО валидный JSON-массив найденных проблем по всем парам, без markdown-обрамления и пояснений, строго в этой форме
(пустой массив [], если проблем нигде не найдено; не включай в ответ пары без проблем):
[
  {{"row": <номер пары из списка выше>, "type": "glossary|register|typo", "severity": "low|medium|high", "message": "конкретное описание проблемы на русском"}}
]"""


def _checks_description(checks: list[str]) -> str | None:
    ai_checks = [c for c in checks if c in CHECK_LABELS]
    if not ai_checks:
        return None
    return "; ".join(CHECK_LABELS[c] for c in ai_checks)


async def _call_claude(prompt: str) -> str | None:
    if not settings.ANTHROPIC_API_KEY:
        return None
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": settings.CLAUDE_MODEL,
                "max_tokens": 8000,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        data = resp.json()

    return next((b["text"] for b in data.get("content", []) if b.get("type") == "text"), None)


def _parse_json_array(text_block: str | None) -> list:
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


async def run_ai_checks(source: str, translation: str, glossary: str, checks: list[str]) -> list[dict]:
    checks_description = _checks_description(checks)
    if not checks_description:
        return []

    prompt = SINGLE_PROMPT.format(
        source=source,
        translation=translation,
        glossary=glossary.strip() or "не указан",
        checks_description=checks_description,
    )
    text_block = await _call_claude(prompt)
    return _parse_json_array(text_block)


async def run_ai_checks_batch(
    items: list[dict],
    glossary: str,
    checks: list[str],
) -> dict[int, list[dict]]:
    """
    items: list of {"context": str, "source": str, "translation": str}, all
    in the same target language. Returns findings keyed by index into items.
    Items with an empty translation are skipped (handled by rule checks as
    "missing translation" instead).
    """
    checks_description = _checks_description(checks)
    if not checks_description:
        return {}

    checkable = [(i, it) for i, it in enumerate(items) if it["translation"].strip()]
    if not checkable:
        return {}

    pairs_block = "\n\n".join(
        f'{n}. Контекст: {it["context"] or "—"}\n'
        f'Источник: """{it["source"]}"""\n'
        f'Перевод: """{it["translation"]}"""'
        for n, (_, it) in enumerate(checkable, start=1)
    )
    prompt = BATCH_PROMPT.format(
        glossary=glossary.strip() or "не указан",
        checks_description=checks_description,
        pairs_block=pairs_block,
    )
    text_block = await _call_claude(prompt)
    raw = _parse_json_array(text_block)

    # Map the 1-based "row" numbers used in the prompt back to the caller's
    # original item indices.
    number_to_index = {n: idx for n, (idx, _) in enumerate(checkable, start=1)}
    grouped: dict[int, list[dict]] = {}
    for entry in raw:
        row_num = entry.get("row")
        idx = number_to_index.get(row_num)
        if idx is None:
            continue
        finding = {k: v for k, v in entry.items() if k != "row"}
        grouped.setdefault(idx, []).append(finding)
    return grouped
