"""
Optional second AI provider — "🌐 Проверить также через Gemini" (see
app.excel_multi's gemini_check plumbing and CheckRunner.tsx's checkbox).

Background (Александр, 2026-09-22): a debug test that ran our own model a
second time with a loosened confidence bar still completely missed a real
Marathi meaning error ("पैज न लावता" = "without placing a bet", when the
source meant "no wagering requirement") — at the time this suggested the
gap wasn't about calibration at all. A later investigation found the actual
cause: BATCH_PROMPT's own cross-row-duplicate instructions were diluting
the model's attention, not a lack of knowledge — see BATCH_PROMPT_SINGLE_ITEM
in claude_client.py and the translation-QA prompt catalog for the full
story (the relaxed-calibration debug mode itself was removed 2026-09-23
once that was settled). None of that changes the value of Gemini as a
genuinely independent second opinion, though: a blind side-by-side test
showed Gemini independently catches that same error (and 4 other real
ones), while correctly staying silent on a genuinely-fine control example —
real evidence that a second, independent model provider can catch what
ours structurally can't, not just re-ask the same model differently.

Deliberately reuses claude_client.build_batch_prompt/parse_json_array/
group_batch_findings/_filter_findings_by_checks as-is: the SAME prompt text
(same calibration, same checks, same instructions) is sent to Gemini as
would be sent to Claude for that language — only which model answers it
differs. That's what makes the comparison meaningful instead of just noise
from two differently-worded prompts.
"""
import httpx

from app.claude_client import (
    build_batch_prompt,
    group_batch_findings,
    parse_json_array,
    _filter_findings_by_checks,
)
from app.config import settings

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# USD per single token — from ai.google.dev/gemini-api/docs/pricing
# (confirmed 2026-09-22). Same safe-fallback philosophy as
# claude_client.MODEL_PRICING_PER_TOKEN: an unpriced/unrecognized model id
# (e.g. after Google renames one — "-preview" model ids are exactly the
# kind that get retired/renamed with little notice) shows cost as $0
# instead of silently guessing at a stale price. Update this table (and
# settings.GEMINI_MODEL, via the GEMINI_MODEL env var on Railway if needed
# without a code change) if that ever happens.
GEMINI_PRICING_PER_TOKEN = {
    "gemini-3.1-pro-preview": {"input": 2.00 / 1_000_000, "output": 12.00 / 1_000_000},
    "gemini-3.8-flash": {"input": 0.75 / 1_000_000, "output": 3.75 / 1_000_000},
    "gemini-3.1-flash-lite": {"input": 0.30 / 1_000_000, "output": 2.50 / 1_000_000},
}


def _gemini_usage_cost(model: str, usage: dict | None) -> float:
    rates = GEMINI_PRICING_PER_TOKEN.get(model)
    if not rates or not usage:
        return 0.0
    return usage.get("input_tokens", 0) * rates["input"] + usage.get("output_tokens", 0) * rates["output"]


def _gemini_error_detail(exc: Exception | None, status_code: int | None, body: str | None) -> str:
    """Turns whatever went wrong into a short, non-technical Russian phrase
    Александр can act on directly from the report itself — added 2026-09-22
    after a real Railway run showed only the generic "ошибка сети, ключа
    или модели" with no way to tell which of the three it actually was
    without SSH-ing into Railway's own logs (which he doesn't have reason
    to check day to day). Deliberately short — this rides inside an
    already-long warning message, not a full stack trace.

    body: Google's own error message text (data["error"]["message"] from
    the JSON error body — see _call_gemini), when one could be extracted.
    Checked FIRST and by content, not by HTTP status code alone — a real
    production run (2026-09-22) showed Google returning plain 400s for
    both a bad API key AND other request problems, so status code alone
    isn't reliable enough to tell those apart; Google's own message text
    is. Falls back to the status-code table below when there's no body
    text, or its wording doesn't match a known pattern — but even then,
    the raw message (if any) is appended so nothing Google actually said
    is thrown away."""
    body_lower = (body or "").lower()
    if body:
        if "api key not valid" in body_lower or "api_key_invalid" in body_lower:
            return "ключ отклонён — Google говорит, что он недействителен (проверьте GEMINI_API_KEY на Railway)"
        if "model" in body_lower and ("not found" in body_lower or "not_found" in body_lower):
            return "модель не найдена — проверьте GEMINI_MODEL на Railway (возможно, устарело название модели)"
        if "quota" in body_lower or "rate limit" in body_lower or "resource_exhausted" in body_lower:
            return "Gemini превысила лимит запросов, попробуйте позже"
        if "permission" in body_lower or "denied" in body_lower:
            return "доступ отклонён — проверьте, что у ключа включён доступ к Gemini API"

    if status_code in (401, 403):
        return "ключ отклонён (проверьте GEMINI_API_KEY на Railway)"
    if status_code == 404:
        return "модель не найдена (проверьте GEMINI_MODEL на Railway, если он задан)"
    if status_code == 429:
        return "Gemini превысила лимит запросов, попробуйте позже"
    if isinstance(status_code, int) and status_code >= 500:
        return "сбой на стороне Google (Gemini временно недоступна)"
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "не удалось подключиться к серверу Gemini (проблема с сетью)"
    if isinstance(exc, httpx.TimeoutException):
        return "Gemini не ответила вовремя (таймаут)"
    if isinstance(exc, ValueError):
        return "Gemini прислала ответ, который не удалось разобрать"
    if status_code is not None:
        base = f"Gemini ответила с ошибкой (код {status_code})"
        # No recognized pattern above, but Google DID send some explanation
        # text — better to show it verbatim (even in English) than hide
        # it, since it's the one clue that could actually pin this down.
        if body:
            return f"{base}: {body[:200]}"
        return base
    return "неизвестная ошибка при обращении к Gemini"


async def _call_gemini(prompt: str, model: str | None = None) -> tuple[str | None, dict, str | None, str | None]:
    """Returns (response_text, usage, stop_reason, error_detail) — the
    first three in the SAME shape as claude_client._call_claude (usage
    normalized to {"input_tokens", "output_tokens"}, stop_reason
    "max_tokens" on truncation) so the rest of the pipeline
    (parse_json_array, cost math, truncation handling) doesn't need to know
    which provider it's talking to. error_detail is only ever set alongside
    stop_reason == "errored" — a short, non-technical reason (see
    _gemini_error_detail) the caller can show directly in its warning
    instead of a generic "something went wrong".

    Unlike _call_claude, network/HTTP failures here are caught and folded
    into the return (text=None, stop_reason="errored") rather than raised —
    this is an OPTIONAL, opt-in add-on pass; a Gemini outage or a bad
    model id must never take down the primary Claude-based check a
    manager actually depends on."""
    if not settings.GEMINI_API_KEY:
        return None, {}, None, None
    resolved_model = model or settings.GEMINI_MODEL
    url = GEMINI_API_URL.format(model=resolved_model)
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                url,
                params={"key": settings.GEMINI_API_KEY},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json"},
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        # Google's own error responses are JSON shaped like
        # {"error": {"code": 400, "message": "...", "status": "..."}} —
        # pulled out here (best-effort; the body might not even be JSON on
        # some failures) so _gemini_error_detail can match on what Google
        # actually said instead of guessing from the status code alone.
        error_message = None
        try:
            error_message = exc.response.json().get("error", {}).get("message")
        except (ValueError, AttributeError):
            pass
        return None, {}, "errored", _gemini_error_detail(exc, exc.response.status_code, error_message)
    except (httpx.HTTPError, ValueError) as exc:
        return None, {}, "errored", _gemini_error_detail(exc, None, None)

    candidates = data.get("candidates") or []
    if not candidates:
        return None, {}, "errored", "Gemini вернула пустой ответ (0 вариантов)"
    parts = candidates[0].get("content", {}).get("parts") or []
    text = next((p["text"] for p in parts if "text" in p), None)
    finish_reason = candidates[0].get("finishReason")
    raw_usage = data.get("usageMetadata") or {}
    usage = {
        "input_tokens": raw_usage.get("promptTokenCount", 0),
        "output_tokens": raw_usage.get("candidatesTokenCount", 0),
    }
    stop_reason = "max_tokens" if finish_reason == "MAX_TOKENS" else finish_reason
    if text is None:
        return None, usage, "errored", "Gemini вернула ответ без текста"
    return text, usage, stop_reason, None


async def run_gemini_checks_batch(
    items: list[dict],
    checks: list[str],
    extra_instructions: str = "",
    target_lang: str = "",
    source_lang: str = "",
) -> tuple[dict[int, list[dict]], float, bool, bool, str | None]:
    """Mirrors claude_client.run_ai_checks_batch's shape and behavior, but
    against Gemini instead of Claude, and with two extra elements: whether
    this call actually reached and got a usable answer from Gemini at all
    (errored), and — only when errored — a short, non-technical reason why
    (error_detail; see _gemini_error_detail). Returns (findings keyed by
    item index, cost_usd, whether the response was truncated by a token
    ceiling, errored, error_detail).

    errored covers: a network/HTTP failure, an unparseable/empty response —
    anything that means "we don't actually know Gemini's opinion on these
    rows", which the caller surfaces as its own small warning (now
    including error_detail, added 2026-09-22 after a real Railway run only
    showed a generic warning with no way to tell what actually went wrong
    without checking Railway's own logs) rather than silently showing zero
    findings as if Gemini looked and found nothing. No GEMINI_API_KEY
    configured is deliberately NOT an error here (mirrors a missing
    ANTHROPIC_API_KEY) — see _call_gemini's own comment."""
    prompt, number_to_index = build_batch_prompt(
        items, checks, extra_instructions, target_lang, source_lang,
    )
    if prompt is None:
        return {}, 0.0, False, False, None

    # _call_gemini itself no-ops (text=None, stop_reason=None) when
    # settings.GEMINI_API_KEY isn't configured — falls through to
    # parse_json_array(None) == [] below, same graceful "nothing to show"
    # as a missing ANTHROPIC_API_KEY, not treated as an error.
    text_block, usage, stop_reason, error_detail = await _call_gemini(prompt)
    if stop_reason == "errored":
        return {}, 0.0, False, True, error_detail

    raw = parse_json_array(text_block)
    grouped = group_batch_findings(raw, number_to_index)
    filtered = {idx: _filter_findings_by_checks(fs, checks) for idx, fs in grouped.items()}
    model = settings.GEMINI_MODEL
    return filtered, _gemini_usage_cost(model, usage), stop_reason == "max_tokens", False, None
