import json
import re

import httpx

from app.config import settings

CHECK_LABELS = {
    # (A "glossary" check used to live here too — required-term matching
    # against an uploaded glossary document. Removed: unlike the other AI
    # checks, that one never actually needed a probabilistic model — a term
    # either matches the glossary or it doesn't, a plain text comparison —
    # so it kept missing/mislabeling things for no good reason. See git
    # history for the removal.)
    "register": "регистр обращения (ты/вы и аналоги) — должен быть единым по всему тексту",
    "typo": (
        "опечатки/ошибки — только те, что искажают смысл (пропущенное отрицание, спутанные число/род, потеря смысла, "
        "грамматика, ломающая понимание); не придирайся к стилю и синонимам с тем же смыслом. Сюда же относится "
        "ДРУГАЯ ВАЛЮТА, чем в исходнике (например, евро вместо доллара, или другой ISO-код) — это меняет смысл "
        "суммы, а не просто стиль"
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

CALIBRATION_BASE = (
    "Общее правило: сообщай, только если уверен(а), что это настоящая ошибка. Сомневаешься или это может быть "
    "допустимым вариантом — не включай. Лучше меньше, но точных находок. Порядок символа валюты относительно числа, "
    "разделители тысяч/десятичных знаков, а также сам порядок частей даты (день/месяц/год) и то, точкой или "
    "слэшем они разделены — это НЕ ошибка перевода сама по себе, и об этом никогда не нужно сообщать."
)

# When "numbers" is also running (a free, 100%-reliable rule check — see
# app.rule_checks.check_numbers — auto-included whenever "Оформление" is
# selected), it already catches every plain digit/date mismatch on its own.
# Telling the AI to still report those under "опечатки/ошибки" too just
# duplicates the same finding twice under two different labels — Александр
# hit exactly this (a wrong year in a date, shown once as "numbers" and
# again, reworded, as "typo"). So when it's running alongside, the AI is
# told to leave plain digits to it and only flag currency IDENTITY (a
# symbol/code that doesn't match — not itself a digit, so "numbers" can't
# catch it). When "numbers" ISN'T selected for this run, the AI keeps
# acting as the only backstop for a wrong number/date, exactly as before.
_CALIBRATION_WITH_NUMBERS_CHECK = (
    "Расхождения в самих цифрах (неверное число, неверная дата и т.п.) уже ловит отдельная бесплатная "
    "автоматическая проверка чисел, включённая в эту проверку — не сообщай о них здесь, даже если заметишь; "
    "в «опечатки/ошибки» сообщай только о несовпадении самой валюты (символ или код, например евро вместо "
    "доллара), а не о цифрах."
)
_CALIBRATION_WITHOUT_NUMBERS_CHECK = (
    "настоящая ошибка — это когда сама валюта или число не совпадают с исходником по смыслу "
    "(см. «опечатки/ошибки»), а не то, как они оформлены."
)


def _calibration(checks: list[str]) -> str:
    tail = _CALIBRATION_WITH_NUMBERS_CHECK if "numbers" in checks else _CALIBRATION_WITHOUT_NUMBERS_CHECK
    return f"{CALIBRATION_BASE} {tail}"


SINGLE_PROMPT = """Ты — модуль контроля качества перевода для бюро переводов. Даны исходный текст и перевод.
Проверяй только критерии из "Что проверять" ниже.

{target_lang_line}

{calibration}

{source_lang_note}

Исходный текст:
\"\"\"{source}\"\"\"

Перевод:
\"\"\"{translation}\"\"\"

Особые указания к задаче (важнее общих правил, если есть):
{extra_instructions}

Что проверять: {checks_description}
Даже если заметишь другую проблему вне этого списка (в т.ч. очевидную и серьёзную) — не включай её в ответ вообще,
ни под каким из перечисленных типов; для неё есть отдельная проверка, которую нужно включить отдельно. Не подгоняй
такую находку под ближайший по смыслу разрешённый тип только потому, что это единственный доступный вариант —
если находка не является настоящим примером именно этого критерия, её не должно быть в ответе.

Верни ТОЛЬКО валидный JSON-массив без markdown и пояснений, строго в этой форме
(пустой массив [], если проблем нет):
[
  {{"type": "{type_enum}", "severity": "low|medium|high", "message": "конкретное описание на русском, с указанием места в тексте, если уместно"}}
]"""

BATCH_PROMPT = """Ты — модуль контроля качества перевода для бюро переводов. Даны пары (контекст, исходный текст, перевод) на один целевой язык.
Проверяй только критерии из "Что проверять" ниже, каждую пару отдельно от остальных.

{target_lang_line}

{calibration}

{source_lang_note}

Особые указания к задаче (важнее общих правил, если есть):
{extra_instructions}

Что проверять: {checks_description}
Даже если заметишь другую проблему вне этого списка (в т.ч. очевидную и серьёзную) — не включай её в ответ вообще,
ни под каким из перечисленных типов; для неё есть отдельная проверка, которую нужно включить отдельно. Не подгоняй
такую находку под ближайший по смыслу разрешённый тип только потому, что это единственный доступный вариант —
если находка не является настоящим примером именно этого критерия, её не должно быть в ответе.

Пары для проверки:
{pairs_block}

Верни ТОЛЬКО валидный JSON-массив по всем парам без markdown и пояснений, строго в этой форме
(пустой массив [], если нигде нет проблем; не включай пары без проблем):
[
  {{"row": <номер пары из списка выше>, "type": "{type_enum}", "severity": "low|medium|high", "message": "конкретное описание на русском"}}
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


def _checks_description(checks: list[str], tone_register: str = "") -> str | None:
    """tone_register comes from the project's actual Tone-of-address
    document for this specific target language (see app.main's per-language
    lookup) — never guessed by the model."""
    ai_checks = [c for c in checks if c in CHECK_LABELS]
    if not ai_checks:
        return None

    labels = []
    for c in ai_checks:
        if c == "register" and tone_register.strip() in ("formal", "informal"):
            word = "формальный (вы/аналог)" if tone_register.strip() == "formal" else "неформальный (ты/аналог)"
            labels.append(f"регистр обращения — для этого языка должен быть {word} по всему тексту")
        else:
            labels.append(CHECK_LABELS[c])

    return "; ".join(labels) if labels else None


def _allowed_ai_types(checks: list[str]) -> set[str]:
    """The finding "type" values this run is actually allowed to return —
    whatever was requested, restricted to the AI check types that exist at
    all. Used as a hard filter on the model's response: the prompt already
    tells the model to check only these, but a model doesn't always listen
    perfectly (a glaring, unrelated problem can slip through anyway), so
    this guarantees a check the manager didn't ask for never shows up in
    the results, rather than just hoping the prompt was followed."""
    return {c for c in checks if c in CHECK_LABELS}


def _filter_findings_by_checks(findings: list[dict], checks: list[str]) -> list[dict]:
    allowed = _allowed_ai_types(checks)
    return [f for f in findings if f.get("type") in allowed]


# Languages that get the stronger CLAUDE_MODEL_HARD instead of the default
# CLAUDE_MODEL — agreed with Александр after costing out the difference
# (Sonnet 4.5 is 3x Haiku 4.5 per token, both input and output, but only
# these languages' calls use it, so the total impact is modest). Matched
# against the BASE language subtag of whatever target_lang a check actually
# runs with, so "kk-KZ", "kk", or any other region variant of Kazakh all
# get it alike.
HARD_LANGUAGE_BASES = {"kk", "ky", "tg", "uz", "sw", "te", "mr", "az"}


def _model_for_lang(target_lang: str) -> str:
    base = target_lang.strip().lower().split("-")[0]
    return settings.CLAUDE_MODEL_HARD if base in HARD_LANGUAGE_BASES else settings.CLAUDE_MODEL


# USD per single token (not per million) — verified against
# platform.claude.com/docs/en/about-claude/pricing. Keyed by the exact
# model id, since that's what actually gets billed; if CLAUDE_MODEL or
# CLAUDE_MODEL_HARD is ever pointed at a model not listed here, cost just
# can't be computed for those calls (see _usage_cost) rather than guessing
# at a price that may no longer be current — update this table when that
# happens, or when Anthropic's prices change.
MODEL_PRICING_PER_TOKEN = {
    "claude-haiku-4-5-20251001": {"input": 1.00 / 1_000_000, "output": 5.00 / 1_000_000},
    "claude-sonnet-4-5-20250929": {"input": 3.00 / 1_000_000, "output": 15.00 / 1_000_000},
}
# The Message Batches API (used for large multi-checks — see
# excel_multi.BATCH_THRESHOLD_CHARS) is half price on both input and output.
BATCH_PRICE_DISCOUNT = 0.5


def _usage_cost(model: str, usage: dict | None, batch: bool = False) -> float:
    """USD cost of one API call from its token usage. Returns 0.0 (rather
    than raising) for an unpriced model or missing usage, so a pricing-table
    gap degrades to "cost not shown" instead of breaking the check itself."""
    rates = MODEL_PRICING_PER_TOKEN.get(model)
    if not rates or not usage:
        return 0.0
    cost = usage.get("input_tokens", 0) * rates["input"] + usage.get("output_tokens", 0) * rates["output"]
    return cost * BATCH_PRICE_DISCOUNT if batch else cost


# Generous headroom for a batch prompt covering many rows of one language
# at once (see excel_multi.build_batch_plan — one prompt per language, not
# per row) — raising this costs nothing by itself (Anthropic bills actual
# tokens generated, not the max_tokens ceiling), and a low ceiling is
# exactly what caused Александр's "incomplete report": a large language's
# response hit the old 8000-token cap mid-array and every finding after
# the cut point was silently lost. Comfortably under both models' real
# output limits (Haiku 4.5: 64K; Sonnet: even higher).
AI_MAX_TOKENS = 32000


async def _call_claude(prompt: str, model: str | None = None) -> tuple[str | None, dict, str | None]:
    """Returns (response_text, usage, stop_reason) — usage is Anthropic's raw
    {"input_tokens": int, "output_tokens": int, ...} dict (empty when no API
    key is configured), used by callers to compute and surface this check's
    actual API cost. stop_reason is "max_tokens" when the response was cut
    off mid-generation (the response is then incomplete/truncated JSON) —
    callers use this to warn rather than silently show a partial result as
    if it were complete."""
    if not settings.ANTHROPIC_API_KEY:
        return None, {}, None
    resolved_model = model or settings.CLAUDE_MODEL
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": resolved_model,
                "max_tokens": AI_MAX_TOKENS,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        data = resp.json()

    text = next((b["text"] for b in data.get("content", []) if b.get("type") == "text"), None)
    return text, data.get("usage", {}), data.get("stop_reason")


def _salvage_json_objects(text: str) -> list:
    """Best-effort recovery when the model's JSON array response got cut off
    mid-array (hit max_tokens) — rather than losing every finding in the
    batch just because the last entry is incomplete, scans for complete
    top-level {...} objects (respecting quoted strings, so a brace inside a
    message string doesn't confuse the count) and parses each on its own,
    keeping whatever came through whole and discarding only the truncated
    tail."""
    objects = []
    depth = 0
    start = None
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                candidate = text[start:i + 1]
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict):
                        objects.append(obj)
                except Exception:
                    pass
                start = None
    return objects


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
    return _salvage_json_objects(cleaned)


def _truncation_warning() -> dict:
    """A synthetic finding (not from the model) injected whenever a
    response was cut off by the max_tokens ceiling — makes an otherwise
    silent, partial result visible instead of just looking like a clean
    "no problems found" report."""
    return {
        "type": "system",
        "severity": "high",
        "message": (
            "Ответ ИИ был обрезан из-за большого объёма материала (слишком много текста и/или находок "
            "для одного запроса) — часть строк могла остаться непроверенной нейросетью. Бесплатные "
            "автоматические проверки (числа, теги, пунктуация) при этом всё равно отработали по всем "
            "строкам. Попробуйте проверить этот язык отдельно от остальных или уменьшить число выбранных "
            "критериев за один прогон."
        ),
    }


_AI_FAILURE_REASONS_RU = {
    "errored": "ошибка на стороне ИИ-сервиса",
    "expired": "истекло время ожидания ответа",
    "canceled": "запрос был отменён",
}


def _ai_failure_warning(reason: str) -> dict:
    """A synthetic finding for when the AI request for a language never
    produced any usable result at all (errored/expired/canceled batch
    request, or a result that never came back) — otherwise this looks
    identical to "checked, nothing found"."""
    return {
        "type": "system",
        "severity": "high",
        "message": (
            f"ИИ-проверка для этого языка не выполнилась ({_AI_FAILURE_REASONS_RU.get(reason, reason)}) — "
            "свяжитесь с нами, чтобы разобраться. Бесплатные автоматические проверки всё равно "
            "отработали по всем строкам."
        ),
    }


async def run_ai_checks(
    source: str, translation: str, checks: list[str], extra_instructions: str = "",
    tone_register: str = "", target_lang: str = "", source_lang: str = "",
) -> tuple[list[dict], float]:
    """Returns (findings, cost_usd) — cost_usd is this one API call's actual
    cost from Anthropic's reported token usage (0.0 when no AI check ran,
    e.g. no API key configured or nothing to check against)."""
    checks_description = _checks_description(checks, tone_register)
    if not checks_description:
        return [], 0.0

    prompt = SINGLE_PROMPT.format(
        target_lang_line=_target_lang_line(target_lang),
        calibration=_calibration(checks),
        source_lang_note=_source_lang_note(source_lang),
        source=source,
        translation=translation,
        extra_instructions=extra_instructions.strip() or "нет",
        checks_description=checks_description,
        type_enum="|".join(sorted(_allowed_ai_types(checks))),
    )
    model = _model_for_lang(target_lang)
    text_block, usage, stop_reason = await _call_claude(prompt, model=model)
    findings = _filter_findings_by_checks(parse_json_array(text_block), checks)
    if stop_reason == "max_tokens":
        findings = findings + [_truncation_warning()]
    return findings, _usage_cost(model, usage)


def build_batch_prompt(
    items: list[dict],
    checks: list[str],
    extra_instructions: str = "",
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
    checks_description = _checks_description(checks, tone_register)
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
        calibration=_calibration(checks),
        source_lang_note=_source_lang_note(source_lang),
        extra_instructions=extra_instructions.strip() or "нет",
        checks_description=checks_description,
        type_enum="|".join(sorted(_allowed_ai_types(checks))),
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
    checks: list[str],
    extra_instructions: str = "",
    tone_register: str = "",
    target_lang: str = "",
    source_lang: str = "",
) -> tuple[dict[int, list[dict]], float, bool]:
    """Synchronous path: builds the prompt, calls Claude right away, and
    returns (findings keyed by index into items, this call's cost_usd,
    whether the response was truncated by the max_tokens ceiling — the
    caller adds a visible warning for that rather than presenting a
    partial result as a complete one)."""
    prompt, number_to_index = build_batch_prompt(
        items, checks, extra_instructions, tone_register, target_lang, source_lang
    )
    if prompt is None:
        return {}, 0.0, False
    model = _model_for_lang(target_lang)
    text_block, usage, stop_reason = await _call_claude(prompt, model=model)
    raw = parse_json_array(text_block)
    grouped = group_batch_findings(raw, number_to_index)
    filtered = {idx: _filter_findings_by_checks(fs, checks) for idx, fs in grouped.items()}
    return filtered, _usage_cost(model, usage), stop_reason == "max_tokens"


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
                "max_tokens": AI_MAX_TOKENS,
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


async def get_batch_results(results_url: str) -> dict[str, dict]:
    """Fetches and parses the batch's .jsonl results. Returns
    {custom_id: {"text": str | None, "usage": dict, "stop_reason": str |
    None, "result_type": str | None}}. text/usage/stop_reason are None/{}/
    None for any request that errored, expired, or was canceled — handled
    rather than crashing the whole multi-check over one bad language, but
    result_type is passed through either way so the caller (see
    excel_multi.finalize_batch_results) can tell that case apart from a
    genuine "checked, nothing found" and surface it instead of staying
    silent about it."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.get(results_url, headers=_headers())
        resp.raise_for_status()
        raw_text = resp.text

    out: dict[str, dict] = {}
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        entry = json.loads(line)
        custom_id = entry.get("custom_id")
        if custom_id is None:
            continue
        result = entry.get("result", {})
        result_type = result.get("type")
        text = None
        usage = {}
        stop_reason = None
        if result_type == "succeeded":
            message = result.get("message", {})
            text = next((b["text"] for b in message.get("content", []) if b.get("type") == "text"), None)
            usage = message.get("usage", {})
            stop_reason = message.get("stop_reason")
        out[custom_id] = {"text": text, "usage": usage, "stop_reason": stop_reason, "result_type": result_type}
    return out
