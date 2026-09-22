"""
Standalone diagnostic tool — deliberately NOT part of the normal check flow.

Background (Александр, 2026-09-22): after we proved Opus genuinely
understands hard-language nuance better than Sonnet on a real side-by-side
comparison (see claude_client.HARD_LANGUAGE_BASES's own comment) — and
then, while discussing how to make hard-language checking more reliable
without Anthropic's newer models offering any way to force a deterministic
answer (see _call_claude's own comment on temperature), several "run a
cheaper model several times instead of one Opus run" ideas came up as a way
to control cost — we had no real data on whether repeated cheaper-model
runs can actually compensate for Opus's demonstrated quality edge on a
real, previously-missed error. Only reasoning on paper, several rounds of
it (2x Sonnet, 4x Haiku, 1x Sonnet + 3x Haiku, ...) with no way to settle
it except by trying it for real.

This module runs one real (context, source, translation) row through each
of MODEL_COMPARISON_CANDIDATES several times each, and reports a hit rate
per model — so the "does asking a cheaper model more than once make up for
it being weaker" question gets answered with real numbers instead of more
guessing.

Deliberately kept OUT of app.excel_multi/run_multi_check: this is not a
feature managers ever see or toggle in the product, and it is not meant to
stay forever — it's a one-off tool for Александр to trigger by hand a
handful of times (e.g. via the backend's interactive /docs page) to gather
comparison data on real known-tricky rows, before we decide whether to
change anything about how hard languages are actually checked in
production. It can be deleted once that decision is made.
"""
import asyncio
import re

import app.claude_client as _claude_client
from app.claude_client import REGISTER_VALUE_TYPE, run_ai_checks_batch
from app.config import settings
from app.excel_multi import AI_CONCURRENCY

# _call_claude/_usage_cost are called through the `_claude_client` module
# object (`_claude_client._call_claude(...)`), not imported by name — a
# `from ... import _call_claude` binding freezes a reference to the
# function object at import time, which smoketest.py's monkeypatching
# (`claude_client_mod._call_claude = fake`) would then silently miss,
# since that only replaces the attribute on app.claude_client itself.
# run_ai_checks_batch above doesn't have this problem: it calls
# _call_claude from WITHIN claude_client.py, so it always resolves
# through that module's own current globals regardless of how it's
# imported here.

# Only these three, fixed, named candidates — never an arbitrary model id
# taken from the request — so a typo in a request can't rack up cost
# against some unpriced/unknown model, and MODEL_PRICING_PER_TOKEN always
# has a rate for whatever actually gets called here. Opus/Sonnet are read
# from settings (not hardcoded ids) so this always compares against
# whatever Railway is actually configured to use in production right now,
# not a stale snapshot; "haiku" has no settings entry of its own any more
# (nothing in the product points at it since the Sonnet switch — see
# config.py's CLAUDE_MODEL comment), so it's named directly here, matching
# its own key in claude_client.MODEL_PRICING_PER_TOKEN.
MODEL_COMPARISON_CANDIDATES = {
    "opus": lambda: settings.CLAUDE_MODEL_HARD,
    "sonnet": lambda: settings.CLAUDE_MODEL,
    "haiku": lambda: "claude-haiku-4-5-20251001",
}

# A hard ceiling on runs_per_model — this hits the real Anthropic API for
# real money on every call, and it's a diagnostic tool reachable without
# the usual project/manager plumbing, so a stray large number in a request
# shouldn't be able to fire off an unbounded number of paid calls.
MAX_RUNS_PER_MODEL = 10


async def _one_run(
    item: dict, checks: list[str], target_lang: str, source_lang: str, model_id: str, semaphore: asyncio.Semaphore,
    relaxed: bool = False,
) -> tuple[list[dict], float, bool]:
    async with semaphore:
        findings_by_idx, cost_usd, truncated = await run_ai_checks_batch(
            [item], checks, target_lang=target_lang, source_lang=source_lang, model_override=model_id,
            relaxed=relaxed,
        )
    # register_value entries aren't a "catch" of anything — they're the
    # register-reporting side-channel (see claude_client.REGISTER_VALUE_TYPE),
    # unrelated to whether this run spotted the actual problem being tested.
    findings = [f for f in findings_by_idx.get(0, []) if f.get("type") != REGISTER_VALUE_TYPE]
    return findings, cost_usd, truncated


# A deliberately MINIMAL prompt, bypassing claude_client.build_batch_prompt
# entirely — no CHECK_LABELS "что считается опечаткой" description, no
# calibration/confidence-bar sentence (strict OR relaxed), no JSON-schema
# instructions, none of the other boilerplate (conciseness instruction,
# type_enum, etc.) that the real production prompt carries. Added
# 2026-09-22: after relaxed=True made zero difference to Sonnet's 0/5 (see
# run_model_comparison's own comment on that result), the leading
# remaining explanation is that the sheer LENGTH/complexity of our normal
# prompt — not the confidence wording specifically — is what's costing
# Sonnet the nuance here, since Александр got a correct answer from Sonnet
# with a bare question shaped almost exactly like this one, outside our
# pipeline entirely. This exists to test THAT, isolated from every other
# variable (still the same row, same target language, same model).
BARE_COMPARISON_PROMPT = (
    "Проверь эту пару «исходник/перевод» на ошибки смысла или грамматики. Ответь строго в таком формате, "
    "ничего не добавляя до или после:\n"
    "ПРОБЛЕМА: да или нет\n"
    "ОБЪЯСНЕНИЕ: если да — в чём именно; если нет — оставь пустым\n\n"
    "Контекст: {context}\n"
    "Исходник ({source_lang}): \"{source}\"\n"
    "Перевод ({target_lang}): \"{translation}\""
)

_BARE_PROBLEM_RE = re.compile(r"ПРОБЛЕМА\s*:\s*(\S+)", re.IGNORECASE)
_BARE_EXPLANATION_RE = re.compile(r"ОБЪЯСНЕНИЕ\s*:\s*(.+)", re.IGNORECASE | re.DOTALL)


def _parse_bare_response(text: str | None) -> list[dict]:
    """Turns a BARE_COMPARISON_PROMPT response into the same finding-dict
    shape _one_run produces, so run_model_comparison's aggregation code
    doesn't need to know which prompt style produced it. No finding at all
    (not even a "system" one) for "ПРОБЛЕМА: нет" or an unparseable
    response — this is a diagnostic probe, not the real product, so a
    model that answers in an unexpected shape just counts as "didn't
    catch it" rather than raising."""
    if not text:
        return []
    match = _BARE_PROBLEM_RE.search(text)
    if not match:
        return []
    # Exact match on "да" (punctuation/whitespace stripped), not a bare
    # "starts with д" prefix check — subagent review (2026-09-22) caught
    # that a prefix check would false-positive on a stray "действительно"
    # or "дно" if a model ever deviates from the requested да/нет format,
    # silently inflating that model's apparent hit rate in the comparison.
    answer = match.group(1).strip().strip("!.,;:").lower()
    if answer != "да":
        return []
    expl_match = _BARE_EXPLANATION_RE.search(text)
    explanation = expl_match.group(1).strip() if expl_match else text.strip()
    return [{"type": "typo", "severity": "medium", "message": explanation[:500]}]


async def _one_run_bare(
    item: dict, target_lang: str, source_lang: str, model_id: str, semaphore: asyncio.Semaphore,
) -> tuple[list[dict], float, bool]:
    prompt = BARE_COMPARISON_PROMPT.format(
        context=item["context"] or "—", source_lang=source_lang or "—", source=item["source"],
        target_lang=target_lang, translation=item["translation"],
    )
    async with semaphore:
        text, usage, stop_reason = await _claude_client._call_claude(prompt, model=model_id)
    findings = _parse_bare_response(text)
    return findings, _claude_client._usage_cost(model_id, usage), stop_reason == "max_tokens"


async def run_model_comparison(
    context: str,
    source: str,
    translation: str,
    target_lang: str,
    source_lang: str = "ru",
    checks: list[str] | None = None,
    runs_per_model: int = 5,
    relaxed: bool = False,
    bare: bool = False,
) -> dict:
    """Runs the exact same (context, source, translation) row through each
    of MODEL_COMPARISON_CANDIDATES, runs_per_model times each, in parallel
    (bounded by a semaphore using the same AI_CONCURRENCY limit the real
    product uses — a fresh Semaphore instance here, not literally shared
    with a concurrent production check, but capped the same way so this
    doesn't hammer Anthropic any harder than a normal check would),
    and returns a per-model hit-rate report: how many of the N runs
    produced ANY finding at all (a "catch"), that model's total cost across
    all its runs, and up to 3 distinct example finding messages from the
    runs that did catch something — so it's visible at a glance whether a
    model is genuinely flagging the SAME real issue being tested, not just
    something unrelated.

    relaxed: forwarded to run_ai_checks_batch/build_batch_prompt — swaps in
    CALIBRATION_RELAXED_OPENING (see claude_client's own comment on it)
    instead of the normal strict "only report if you're sure" confidence
    bar. Added 2026-09-22 after Александр got a real Sonnet answer OUTSIDE
    our pipeline (a bare, unstructured question with no confidence-bar
    wording at all) that correctly caught the same Marathi row our own
    strict-calibration Sonnet run missed 5 times in a row — real evidence
    that at least SOME of the gap can be our own prompt's confidence bar
    making Sonnet second-guess itself, not necessarily Sonnet lacking the
    underlying knowledge outright. This flag exists to test exactly that,
    with real numbers, before concluding either way. In practice
    (2026-09-22): relaxed=True made ZERO difference to Sonnet (still 0/5),
    which argues AGAINST the confidence-bar theory for Sonnet specifically
    — see "bare" below for the hypothesis this result points to instead.

    bare: bypasses claude_client's whole prompt-building machinery
    (build_batch_prompt/run_ai_checks_batch — no CHECK_LABELS description,
    no calibration wording at all, no JSON-schema instructions) in favor
    of BARE_COMPARISON_PROMPT, a minimal question shaped like the one
    Александр asked Sonnet directly outside our pipeline (and which DID
    get a correct answer). Tests whether it's our prompt's sheer
    length/complexity — not the confidence bar specifically — costing
    Sonnet the nuance. When True, relaxed is ignored (irrelevant — the
    bare prompt has no calibration wording to swap) and the report's
    "relaxed" field is forced to False for clarity.

    Returns {} when no ANTHROPIC_API_KEY is configured — same graceful
    no-op as the rest of the AI-check pipeline, rather than an error."""
    if not settings.ANTHROPIC_API_KEY:
        return {}
    checks = checks or ["typo"]
    runs_per_model = max(1, min(runs_per_model, MAX_RUNS_PER_MODEL))
    item = {"context": context, "source": source, "translation": translation}
    semaphore = asyncio.Semaphore(AI_CONCURRENCY)
    relaxed = False if bare else relaxed

    results = {}
    total_cost = 0.0
    for name, resolve_model in MODEL_COMPARISON_CANDIDATES.items():
        model_id = resolve_model()
        if bare:
            runs = await asyncio.gather(*[
                _one_run_bare(item, target_lang, source_lang, model_id, semaphore)
                for _ in range(runs_per_model)
            ])
        else:
            runs = await asyncio.gather(*[
                _one_run(item, checks, target_lang, source_lang, model_id, semaphore, relaxed=relaxed)
                for _ in range(runs_per_model)
            ])
        catches = 0
        examples: list[str] = []
        model_cost = 0.0
        any_truncated = False
        for findings, cost_usd, truncated in runs:
            model_cost += cost_usd
            any_truncated = any_truncated or truncated
            if findings:
                catches += 1
                for f in findings:
                    msg = f.get("message", "")
                    if msg and msg not in examples and len(examples) < 3:
                        examples.append(msg)
        model_cost = round(model_cost, 4)
        total_cost += model_cost
        results[name] = {
            "model": model_id,
            "runs": runs_per_model,
            "catches": catches,
            "hit_rate": round(catches / runs_per_model, 2),
            "cost_usd": model_cost,
            "example_messages": examples,
            "truncated": any_truncated,
        }

    # Rounded from the already-rounded per-model costs (not the raw,
    # unrounded totals) so total_cost_usd always matches exactly what you
    # get by adding up the three cost_usd figures printed right above it —
    # a reader doing that arithmetic by hand should never see it come out
    # a fraction of a cent short.
    report = {
        "target_lang": target_lang,
        "checks": checks,
        "relaxed": relaxed,
        "bare": bare,
        "total_cost_usd": round(total_cost, 4),
        "results": results,
    }
    report["summary_ru"] = _format_summary_ru(report)
    return report


# NAMES_RU keeps the plain-text summary readable without exposing the raw
# internal candidate keys ("opus"/"sonnet"/"haiku" already read fine in
# Russian as-is, but spelled out this way avoids any ambiguity about which
# is which if the candidate list ever grows).
_NAMES_RU = {"opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku"}


def _format_summary_ru(report: dict) -> str:
    """A ready-to-read Russian summary of the comparison, so the result can
    be understood at a glance in the /docs response without translating
    JSON field names by hand."""
    if report.get("bare"):
        mode = " (🧪 упрощённый прямой вопрос, без нашего обычного промпта)"
    elif report.get("relaxed"):
        mode = " (🔬 сниженная планка уверенности)"
    else:
        mode = ""
    lines = [f"Сравнение моделей для языка {report['target_lang']}{mode}:"]
    for name, r in report["results"].items():
        label = _NAMES_RU.get(name, name)
        lines.append(
            f"— {label}: поймала {r['catches']} из {r['runs']} прогонов "
            f"({int(r['hit_rate'] * 100)}%), стоимость ${r['cost_usd']:.4f}"
            + (" [ответ был обрезан хотя бы раз]" if r["truncated"] else "")
        )
    lines.append(f"Итого потрачено на весь тест: ${report['total_cost_usd']:.4f}")
    return "\n".join(lines)
