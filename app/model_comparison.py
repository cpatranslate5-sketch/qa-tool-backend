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
from app.claude_client import REGISTER_VALUE_TYPE
from app.config import settings
from app.excel_multi import AI_CONCURRENCY

# _call_claude/_usage_cost/build_batch_prompt/group_batch_findings/
# parse_json_array/_filter_findings_by_checks are called through the
# `_claude_client` module object (`_claude_client._call_claude(...)`), not
# imported by name — a `from ... import _call_claude` binding freezes a
# reference to the function object at import time, which smoketest.py's
# monkeypatching (`claude_client_mod._call_claude = fake`) would then
# silently miss, since that only replaces the attribute on
# app.claude_client itself.
#
# _one_run used to just call claude_client.run_ai_checks_batch directly —
# simpler, but it only ever returns the ALREADY-FILTERED findings
# (whatever survived _filter_findings_by_checks, which silently drops any
# finding whose "type" isn't one this run is actually allowed to return —
# see that function's own comment). That filter is a deliberate safety net
# in production, but it means a real miss and "the model found it but
# under a type we then discarded" look IDENTICAL from the outside — and
# Александр's 2026-09-23 Kyrgyz/French investigation kept running into
# exactly this uncertainty with no way to settle it. _one_run below now
# rebuilds run_ai_checks_batch's own steps by hand (same prompt, same
# call, same parsing) so it can hand back the model's RAW parsed response
# too, before that filter ever runs.

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

# Default subset of MODEL_COMPARISON_CANDIDATES a call runs when "models"
# isn't given — Александр's ask, 2026-09-23: Opus is real money and he
# doesn't want it included just for a routine comparison run any more (it
# already proved its edge once — see HARD_LANGUAGE_BASES's own comment).
# Still selectable by name via the "models" parameter when a comparison
# against Opus specifically is actually wanted.
DEFAULT_COMPARISON_MODELS = ["sonnet", "haiku"]

# A hard ceiling on runs_per_model — this hits the real Anthropic API for
# real money on every call, and it's a diagnostic tool reachable without
# the usual project/manager plumbing, so a stray large number in a request
# shouldn't be able to fire off an unbounded number of paid calls.
MAX_RUNS_PER_MODEL = 10


async def _run_search_step(
    item: dict, target_lang: str, source_lang: str, model_id: str, semaphore: asyncio.Semaphore,
) -> tuple[dict[int, list[str]], float]:
    """Step 1 of the two-step pipeline (claude_client.FINDINGS_SEARCH_PROMPT
    — see that constant's own comment), run under THIS specific candidate
    model_id rather than claude_client._search_findings' own
    _model_for_lang(target_lang) choice — this diagnostic exists precisely
    to compare Opus/Sonnet/Haiku against each other, so Step 1 needs to run
    under whichever model this particular comparison run is testing, not
    always production's own choice. Rebuilt from claude_client's own
    internal pieces (module-attribute access, not `from` imports — see
    this module's own top-of-file comment for why) rather than calling
    _search_findings directly, same reasoning as _one_run below."""
    checkable = _claude_client._checkable_items([item])
    if not checkable:
        return {}, 0.0
    prompt = _claude_client.FINDINGS_SEARCH_PROMPT.format(
        target_lang_line=_claude_client._target_lang_line(target_lang),
        source_lang_note=_claude_client._source_lang_note(source_lang),
        pairs_block=_claude_client._pairs_block(checkable),
    )
    async with semaphore:
        text_block, usage, _stop_reason = await _claude_client._call_claude(prompt, model=model_id)
    return (
        _claude_client._parse_search_findings(text_block, checkable),
        _claude_client._usage_cost(model_id, usage),
    )


async def _one_run(
    item: dict, checks: list[str], target_lang: str, source_lang: str, model_id: str, semaphore: asyncio.Semaphore,
    two_step: bool = False,
) -> tuple[list[dict], float, bool, list]:
    """Same (context, source, translation) row, same prompt, same model call
    as production's run_ai_checks_batch([item], ...) — rebuilt by hand
    instead of calling that function directly so the 4th return value can
    be the model's RAW parsed JSON array, exactly as it came back, before
    _filter_findings_by_checks silently drops anything whose "type" isn't
    one this run is actually allowed to return. See this module's own
    top-of-file comment for why that distinction matters.

    two_step: when true, runs claude_client's Step 1 (_run_search_step
    above) first, under this same model_id, and feeds its candidates into
    the structured prompt exactly as production's run_ai_checks_batch now
    does — so this diagnostic can show whether the two-step pipeline
    actually closes a real, previously-missed gap for a given model,
    before that pipeline is trusted in production. Search cost is folded
    into this run's own returned cost_usd, same as production does."""
    search_cost = 0.0
    prior_findings: dict[int, list[str]] | None = None
    if two_step:
        prior_findings, search_cost = await _run_search_step(item, target_lang, source_lang, model_id, semaphore)
    prompt, number_to_index = _claude_client.build_batch_prompt(
        [item], checks, target_lang=target_lang, source_lang=source_lang, prior_findings=prior_findings,
    )
    if prompt is None:
        return [], search_cost, False, []
    async with semaphore:
        text_block, usage, stop_reason = await _claude_client._call_claude(prompt, model=model_id)
    raw = _claude_client.parse_json_array(text_block)
    grouped = _claude_client.group_batch_findings(raw, number_to_index)
    checked = _claude_client._filter_findings_by_checks(grouped.get(0, []), checks)
    # register_value entries aren't a "catch" of anything — they're the
    # register-reporting side-channel (see claude_client.REGISTER_VALUE_TYPE),
    # unrelated to whether this run spotted the actual problem being tested.
    findings = [f for f in checked if f.get("type") != REGISTER_VALUE_TYPE]
    cost_usd = search_cost + _claude_client._usage_cost(model_id, usage)
    return findings, cost_usd, stop_reason == "max_tokens", raw


# A deliberately MINIMAL prompt, bypassing claude_client.build_batch_prompt
# entirely — no CHECK_LABELS "что считается опечаткой" description, no
# calibration/confidence-bar sentence, no JSON-schema instructions, none of
# the other boilerplate (conciseness instruction, type_enum, etc.) that the
# real production prompt carries. Added 2026-09-22 after a relaxed-
# confidence-bar test made zero difference to Sonnet's 0/5 result, pointing
# away from confidence wording and toward the sheer LENGTH/complexity of
# our normal prompt as what was costing Sonnet the nuance here — Александр
# got a correct answer from Sonnet with a bare question shaped almost
# exactly like this one, outside our pipeline entirely. This exists to
# test THAT, isolated from every other variable (still the same row, same
# target language, same model). Confirmed right: see
# claude_client.BATCH_PROMPT_SINGLE_ITEM, the fix that came out of this.
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
    bare: bool = False,
    two_step: bool = False,
    models: list[str] | None = None,
) -> dict:
    """Runs the exact same (context, source, translation) row through each
    of the selected candidate models, runs_per_model times each, in
    parallel (bounded by a semaphore using the same AI_CONCURRENCY limit
    the real product uses — a fresh Semaphore instance here, not literally
    shared with a concurrent production check, but capped the same way so
    this doesn't hammer Anthropic any harder than a normal check would),
    and returns a per-model hit-rate report: how many of the N runs
    produced ANY finding at all (a "catch"), that model's total cost across
    all its runs, and up to 3 distinct example finding messages from the
    runs that did catch something — so it's visible at a glance whether a
    model is genuinely flagging the SAME real issue being tested, not just
    something unrelated.

    bare: bypasses claude_client's whole prompt-building machinery
    (build_batch_prompt/run_ai_checks_batch — no CHECK_LABELS description,
    no calibration wording at all, no JSON-schema instructions) in favor
    of BARE_COMPARISON_PROMPT, a minimal question shaped like the one
    Александр asked Sonnet directly outside our pipeline (and which DID
    get a correct answer). Tests whether it's our prompt's sheer
    length/complexity — not the confidence bar — costing a model the
    nuance.

    two_step: runs claude_client's two-step search-then-check pipeline
    (see FINDINGS_SEARCH_PROMPT's own comment) instead of the plain
    structured prompt — an open, schema-free search pass first, whose
    candidates then feed into the same structured prompt `bare=False`
    already uses. Mutually exclusive with `bare` in practice (bare skips
    the structured prompt entirely, so there's nothing for a search step
    to feed into) — if both are set, `bare` wins and two_step is ignored,
    since bare's whole point is testing the structured prompt's machinery
    in isolation. Added 2026-09-23 to verify the pipeline for real, on the
    same known Kyrgyz/French examples this investigation has been using,
    before it's wired into production's own run_ai_checks/run_ai_checks_batch.

    models: which of MODEL_COMPARISON_CANDIDATES to actually run, by name
    ("opus"/"sonnet"/"haiku"). Defaults to DEFAULT_COMPARISON_MODELS
    (sonnet + haiku, no Opus — Александр's ask, 2026-09-23, to not spend on
    Opus for a routine comparison). An unknown name is silently dropped
    rather than erroring, same graceful-degradation spirit as the rest of
    this tool; an empty/all-unknown result falls back to the default too,
    so a bad request still returns a useful comparison instead of nothing.

    Returns {} when no ANTHROPIC_API_KEY is configured — same graceful
    no-op as the rest of the AI-check pipeline, rather than an error."""
    if not settings.ANTHROPIC_API_KEY:
        return {}
    checks = checks or ["typo"]
    runs_per_model = max(1, min(runs_per_model, MAX_RUNS_PER_MODEL))
    item = {"context": context, "source": source, "translation": translation}
    semaphore = asyncio.Semaphore(AI_CONCURRENCY)

    selected_names = [m for m in (models or DEFAULT_COMPARISON_MODELS) if m in MODEL_COMPARISON_CANDIDATES]
    if not selected_names:
        selected_names = DEFAULT_COMPARISON_MODELS

    results = {}
    total_cost = 0.0
    for name in selected_names:
        model_id = MODEL_COMPARISON_CANDIDATES[name]()
        if bare:
            runs = await asyncio.gather(*[
                _one_run_bare(item, target_lang, source_lang, model_id, semaphore)
                for _ in range(runs_per_model)
            ])
        else:
            runs = await asyncio.gather(*[
                _one_run(item, checks, target_lang, source_lang, model_id, semaphore, two_step=two_step)
                for _ in range(runs_per_model)
            ])
        catches = 0
        examples: list[str] = []
        # The model's raw, unfiltered JSON response for every run (empty
        # for bare mode — there's no schema/type filter to look behind
        # there, BARE_COMPARISON_PROMPT's plain да/нет answer already shows
        # everything). Kept even for a run with zero real "catches" — a
        # non-empty raw entry alongside an empty findings list is the exact
        # signal that the model DID respond with something, just not under
        # a "type" this run was allowed to return (see _filter_findings_by_checks),
        # rather than a genuine miss. Александр's ask, 2026-09-23, after the
        # Kyrgyz/French investigation kept running into this exact
        # uncertainty with no way to tell the two apart from outside.
        raw_responses: list[list] = []
        model_cost = 0.0
        any_truncated = False
        for run in runs:
            findings, cost_usd, truncated = run[0], run[1], run[2]
            model_cost += cost_usd
            any_truncated = any_truncated or truncated
            if len(run) > 3:
                raw_responses.append(run[3])
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
            "raw_responses": raw_responses,
        }

    # Rounded from the already-rounded per-model costs (not the raw,
    # unrounded totals) so total_cost_usd always matches exactly what you
    # get by adding up the three cost_usd figures printed right above it —
    # a reader doing that arithmetic by hand should never see it come out
    # a fraction of a cent short.
    report = {
        "target_lang": target_lang,
        "checks": checks,
        "bare": bare,
        "two_step": two_step and not bare,
        "models": selected_names,
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
    elif report.get("two_step"):
        mode = " (🔎 в два шага: сначала свободный поиск, потом обычная проверка)"
    else:
        mode = ""
    lines = [f"Сравнение моделей для языка {report['target_lang']}{mode}:"]
    for name, r in report["results"].items():
        label = _NAMES_RU.get(name, name)
        # A model that caught NOTHING but still sent back non-empty raw JSON
        # on at least one run said SOMETHING — it just didn't survive
        # _filter_findings_by_checks (wrong/unrecognized "type"), which
        # looks identical to a genuine miss unless raw_responses is checked
        # too. Flagged here so this isn't only visible by opening the raw
        # data by hand.
        silent_drop = r["catches"] == 0 and any(r.get("raw_responses") or [])
        lines.append(
            f"— {label}: поймала {r['catches']} из {r['runs']} прогонов "
            f"({int(r['hit_rate'] * 100)}%), стоимость ${r['cost_usd']:.4f}"
            + (" [ответ был обрезан хотя бы раз]" if r["truncated"] else "")
            + (" [⚠ модель что-то ответила, но это не прошло фильтр по типу — см. raw_responses]" if silent_drop else "")
        )
    lines.append(f"Итого потрачено на весь тест: ${report['total_cost_usd']:.4f}")
    return "\n".join(lines)
