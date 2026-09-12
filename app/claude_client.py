import json
import re

import httpx

from app.config import settings

CHECK_LABELS = {
    # Purely term-matching now — number/currency FORMAT lives entirely in
    # the separate "numerals" check below, driven by the project's actual
    # Numerals document rather than anything the model has to guess.
    "glossary": "глоссарий — термины переводить ровно как в глоссарии",
    # Base wording for when no Numerals-doc rule exists for this language
    # (см. _checks_description — normally replaced by the real rule text,
    # so the model is never left guessing at a format on its own).
    "numerals": "формат чисел и валют — строго по примеру/правилу для этого языка (см. ниже)",
    "register": "регистр обращения (ты/вы и аналоги) — должен быть единым по всему тексту",
    "typo": (
        "опечатки/ошибки — только те, что искажают смысл (пропущенное отрицание, спутанные число/род, потеря смысла, "
        "грамматика, ломающая понимание); не придирайся к стилю и синонимам с тем же смыслом"
    ),
    "untranslatable": (
        "непереводимые термины — имена турниров/игр/брендов/продуктов. Сообщай, только если термин в переводе изменён, "
        "переведён по смыслу или с ошибкой; транслитерация и падежные окончания — не ошибка, обычные слова не считаются. "
        "При конфликте с «Особыми указаниями» ниже — следуй им"
    ),
    "completeness": (
        "неполнота перевода — куски исходного текста, оставшиеся непереведёнными внутри перевода, ИЛИ перевод целиком "
        "на другом языке, чем требуемый целевой (например, вставлен не тот язык, или перевод не изменился с другого "
        "родственного языка). Не путать с пустым переводом (отдельная проверка) или с иной длиной перевода — сама по "
        "себе длина не проблема"
    ),
}

CALIBRATION = (
    "Общее правило: сообщай, только если уверен(а), что это настоящая ошибка. Сомневаешься или это может быть "
    "допустимым вариантом — не включай. Лучше меньше, но точных находок. Порядок символа валюты относительно числа, "
    "разделители тысяч/десятичных знаков и подобное оформление чисел — это НЕ ошибка перевода сама по себе; "
    "сообщай об этом, только по проверке «формат чисел и валют» и только если это прямо противоречит указанному "
    "правилу для этого языка, а не по общим представлениям о формате."
)


SINGLE_PROMPT = """Ты — модуль контроля качества перевода для бюро переводов. Даны исходный текст и перевод.
Проверяй только критерии из "Что проверять" ниже.

{target_lang_line}

{calibration}

{source_lang_note}

Исходный текст:
\"\"\"{source}\"\"\"

Перевод:
\"\"\"{translation}\"\"\"

Глоссарий (обязательные соответствия и формат чисел, если есть):
{glossary}

Особые указания к задаче (важнее общих правил, если есть):
{extra_instructions}

Что проверять: {checks_description}

Верни ТОЛЬКО валидный JSON-массив без markdown и пояснений, строго в этой форме
(пустой массив [], если проблем нет):
[
  {{"type": "glossary|numerals|register|typo|untranslatable|completeness", "severity": "low|medium|high", "message": "конкретное описание на русском, с указанием места в тексте, если уместно"}}
]"""

BATCH_PROMPT = """Ты — модуль контроля качества перевода для бюро переводов. Даны пары (контекст, исходный текст, перевод) на один целевой язык.
Проверяй только критерии из "Что проверять" ниже, каждую пару отдельно от остальных.

{target_lang_line}

{calibration}

{source_lang_note}

Глоссарий (обязательные соответствия и формат чисел, если есть, для всех пар):
{glossary}

Особые указания к задаче (важнее общих правил, если есть):
{extra_instructions}

Что проверять: {checks_description}

Пары для проверки:
{pairs_block}

Верни ТОЛЬКО валидный JSON-массив по всем парам без markdown и пояснений, строго в этой форме
(пустой массив [], если нигде нет проблем; не включай пары без проблем):
[
  {{"row": <номер пары из списка выше>, "type": "glossary|numerals|register|typo|untranslatable|completeness", "severity": "low|medium|high", "message": "конкретное описание на русском"}}
]"""


def _source_lang_note(source_lang: str) -> str:
    """Client-specific rule: when the source is Russian, English words or
    phrases embedded in it (brand names, terms, rare exceptions aside)
    should stay in English in every target translation too — not be
    translated into the target language."""
    if source_lang.strip().lower() != "ru":
        return ""
    return (
        "Особое правило: если в русском исходнике есть слова или фразы на английском (не считая редких "
        "исключений), они должны остаться на английском и в переводе на другой язык — не переводиться. Если такой "
        "фрагмент всё же переведён на язык перевода, это ошибка (относи к «неполнота перевода»)."
    )


def _target_lang_line(target_lang: str) -> str:
    """Explicitly names the target language rather than leaving the model
    to infer it purely from the translated text — closely related
    languages (e.g. Turkish/Azerbaijani, Kazakh/Kyrgyz) are otherwise a
    real risk of being mixed up, especially in short texts."""
    code = target_lang.strip().lower()
    if not code:
        return ""
    return f"Целевой язык перевода: {code}. Ориентируйся конкретно на этот язык — не путай с родственными языками."


def _format_numeral_rule(fields: dict | None) -> str:
    """Turns the project's per-language Numerals fields (e.g. {"формат
    даты": "16.08.2023", "разделитель дробной части": "—,50", ...}) into a
    single readable line for the prompt."""
    if not fields:
        return ""
    return "; ".join(f"{label} — {value}" for label, value in fields.items())


def _checks_description(checks: list[str], numeral_rule: dict | None = None, tone_register: str = "") -> str | None:
    """numeral_rule and tone_register come from the project's actual
    Numerals/Tone-of-address documents for this specific target language
    (see app.main's per-language lookups) — never guessed by the model.

    If "numerals" is selected but there's no rule for this language, it's
    dropped from the description entirely rather than left as a vague
    instruction — same principle as before: never give the model an idea
    it has nothing concrete to check against."""
    ai_checks = [c for c in checks if c in CHECK_LABELS]
    if not ai_checks:
        return None

    numeral_text = _format_numeral_rule(numeral_rule)
    labels = []
    for c in ai_checks:
        if c == "numerals":
            if not numeral_text:
                continue
            labels.append(
                f'формат чисел и валют — строго по правилам для этого языка: "{numeral_text}"; '
                f"не по общим представлениям о формате"
            )
        elif c == "register" and tone_register.strip() in ("formal", "informal"):
            word = "формальный (вы/аналог)" if tone_register.strip() == "formal" else "неформальный (ты/аналог)"
            labels.append(f"регистр обращения — для этого языка должен быть {word} по всему тексту")
        else:
            labels.append(CHECK_LABELS[c])

    return "; ".join(labels) if labels else None


# Languages that get the stronger CLAUDE_MODEL_HARD instead of the default
# CLAUDE_MODEL — agreed with Александр after costing out the difference
# (Sonnet is exactly ~2x Haiku per token, but only these languages' calls
# use it, so the total impact is modest). Matched against the BASE
# language subtag of whatever target_lang a check actually runs with, so
# "kk-KZ", "kk", or any other region variant of Kazakh all get it alike.
HARD_LANGUAGE_BASES = {"kk", "ky", "tg", "uz", "sw", "te", "mr", "az"}


def _model_for_lang(target_lang: str) -> str:
    base = target_lang.strip().lower().split("-")[0]
    return settings.CLAUDE_MODEL_HARD if base in HARD_LANGUAGE_BASES else settings.CLAUDE_MODEL


async def _call_claude(prompt: str, model: str | None = None) -> str | None:
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
                "model": model or settings.CLAUDE_MODEL,
                "max_tokens": 8000,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        data = resp.json()

    return next((b["text"] for b in data.get("content", []) if b.get("type") == "text"), None)


def parse_json_array(text_block: str | None) -> list:
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


async def run_ai_checks(
    source: str, translation: str, glossary: str, checks: list[str], extra_instructions: str = "",
    numeral_rule: dict | None = None, tone_register: str = "", target_lang: str = "", source_lang: str = "",
) -> list[dict]:
    checks_description = _checks_description(checks, numeral_rule, tone_register)
    if not checks_description:
        return []

    prompt = SINGLE_PROMPT.format(
        target_lang_line=_target_lang_line(target_lang),
        calibration=CALIBRATION,
        source_lang_note=_source_lang_note(source_lang),
        source=source,
        translation=translation,
        glossary=glossary.strip() or "не указан",
        extra_instructions=extra_instructions.strip() or "нет",
        checks_description=checks_description,
    )
    text_block = await _call_claude(prompt, model=_model_for_lang(target_lang))
    return parse_json_array(text_block)


def build_batch_prompt(
    items: list[dict],
    glossary: str,
    checks: list[str],
    extra_instructions: str = "",
    numeral_rule: dict | None = None,
    tone_register: str = "",
    target_lang: str = "",
    source_lang: str = "",
) -> tuple[str | None, dict[int, int]]:
    """
    Builds the prompt for one language's batch of (context, source,
    translation) triples, without calling the API — shared by the
    synchronous path (run_ai_checks_batch, below) and the Message Batches
    path (excel_multi.build_batch_plan), so both send an identical prompt
    for the same input.

    items: list of {"context": str, "source": str, "translation": str}, all
    in the same target language. Items with an empty translation are
    skipped (handled by rule checks as "missing translation" instead).

    Returns (prompt, number_to_index) — prompt is None when there's nothing
    to ask the AI (no AI check types selected, or nothing checkable).
    number_to_index maps the 1-based "row" numbers used inside the prompt
    back to the caller's original item indices — pass it to
    group_batch_findings once you have the model's response.
    """
    checks_description = _checks_description(checks, numeral_rule, tone_register)
    if not checks_description:
        return None, {}

    checkable = [(i, it) for i, it in enumerate(items) if it["translation"].strip()]
    if not checkable:
        return None, {}

    pairs_block = "\n\n".join(
        f'{n}. Контекст: {it["context"] or "—"}\n'
        f'Источник: """{it["source"]}"""\n'
        f'Перевод: """{it["translation"]}"""'
        for n, (_, it) in enumerate(checkable, start=1)
    )
    prompt = BATCH_PROMPT.format(
        target_lang_line=_target_lang_line(target_lang),
        calibration=CALIBRATION,
        source_lang_note=_source_lang_note(source_lang),
        glossary=glossary.strip() or "не указан",
        extra_instructions=extra_instructions.strip() or "нет",
        checks_description=checks_description,
        pairs_block=pairs_block,
    )
    number_to_index = {n: idx for n, (idx, _) in enumerate(checkable, start=1)}
    return prompt, number_to_index


def group_batch_findings(raw: list, number_to_index: dict[int, int]) -> dict[int, list[dict]]:
    """Maps the model's {"row": n, ...} entries back to the caller's item
    indices via the number_to_index from build_batch_prompt."""
    grouped: dict[int, list[dict]] = {}
    for entry in raw:
        row_num = entry.get("row")
        idx = number_to_index.get(row_num)
        if idx is None:
            continue
        finding = {k: v for k, v in entry.items() if k != "row"}
        grouped.setdefault(idx, []).append(finding)
    return grouped


async def run_ai_checks_batch(
    items: list[dict],
    glossary: str,
    checks: list[str],
    extra_instructions: str = "",
    numeral_rule: dict | None = None,
    tone_register: str = "",
    target_lang: str = "",
    source_lang: str = "",
) -> dict[int, list[dict]]:
    """Synchronous path: builds the prompt, calls Claude right away, and
    returns findings keyed by index into items."""
    prompt, number_to_index = build_batch_prompt(
        items, glossary, checks, extra_instructions, numeral_rule, tone_register, target_lang, source_lang
    )
    if prompt is None:
        return {}
    text_block = await _call_claude(prompt, model=_model_for_lang(target_lang))
    raw = parse_json_array(text_block)
    return group_batch_findings(raw, number_to_index)


# --------------------------------------------------- Message Batches API ---
# Used for large multi-checks (see excel_multi.BATCH_THRESHOLD_CHARS): all
# per-language requests for one upload are submitted together as a single
# Anthropic batch job at half the normal per-token price. Results usually
# land within an hour rather than immediately — app.main polls for them.

BATCHES_URL = "https://api.anthropic.com/v1/messages/batches"


def _headers() -> dict:
    return {
        "x-api-key": settings.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }


async def create_message_batch(requests: list[dict]) -> str | None:
    """requests: list of {"custom_id": str, "prompt": str, "model": str
    (optional)}. Submits them all as one Anthropic Message Batch — each
    request can specify its own model (see excel_multi.build_batch_plan,
    which sets the per-language model via _model_for_lang), falling back to
    the default CLAUDE_MODEL when omitted — and returns the batch id, or
    None if there's no API key configured or nothing to submit."""
    if not settings.ANTHROPIC_API_KEY or not requests:
        return None
    batch_requests = [
        {
            "custom_id": r["custom_id"],
            "params": {
                "model": r.get("model") or settings.CLAUDE_MODEL,
                "max_tokens": 8000,
                "messages": [{"role": "user", "content": r["prompt"]}],
            },
        }
        for r in requests
    ]
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(BATCHES_URL, headers=_headers(), json={"requests": batch_requests})
        resp.raise_for_status()
        return resp.json()["id"]


async def get_batch_status(batch_id: str) -> dict:
    """Raw batch object from Anthropic — notably processing_status
    ("in_progress" | "ended" | "canceling") and results_url (set once ended)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{BATCHES_URL}/{batch_id}", headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def get_batch_results(results_url: str) -> dict[str, str | None]:
    """Fetches and parses the batch's .jsonl results. Returns
    {custom_id: text_block}, with None for any request that errored,
    expired, or was canceled (extremely unlikely, but handled rather than
    crashing the whole multi-check over one bad language)."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.get(results_url, headers=_headers())
        resp.raise_for_status()
        raw_text = resp.text

    out: dict[str, str | None] = {}
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        entry = json.loads(line)
        custom_id = entry.get("custom_id")
        if custom_id is None:
            continue
        result = entry.get("result", {})
        text = None
        if result.get("type") == "succeeded":
            message = result.get("message", {})
            text = next((b["text"] for b in message.get("content", []) if b.get("type") == "text"), None)
        out[custom_id] = text
    return out
