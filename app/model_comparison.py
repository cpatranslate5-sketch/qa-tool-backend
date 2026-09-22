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

from app.claude_client import REGISTER_VALUE_TYPE, run_ai_checks_batch
from app.config import settings
from app.excel_multi import AI_CONCURRENCY

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
) -> tuple[list[dict], float, bool]:
    async with semaphore:
        findings_by_idx, cost_usd, truncated = await run_ai_checks_batch(
            [item], checks, target_lang=target_lang, source_lang=source_lang, model_override=model_id,
        )
    # register_value entries aren't a "catch" of anything — they're the
    # register-reporting side-channel (see claude_client.REGISTER_VALUE_TYPE),
    # unrelated to whether this run spotted the actual problem being tested.
    findings = [f for f in findings_by_idx.get(0, []) if f.get("type") != REGISTER_VALUE_TYPE]
    return findings, cost_usd, truncated


async def run_model_comparison(
    context: str,
    source: str,
    translation: str,
    target_lang: str,
    source_lang: str = "ru",
    checks: list[str] | None = None,
    runs_per_model: int = 5,
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

    Returns {} when no ANTHROPIC_API_KEY is configured — same graceful
    no-op as the rest of the AI-check pipeline, rather than an error."""
    if not settings.ANTHROPIC_API_KEY:
        return {}
    checks = checks or ["typo"]
    runs_per_model = max(1, min(runs_per_model, MAX_RUNS_PER_MODEL))
    item = {"context": context, "source": source, "translation": translation}
    semaphore = asyncio.Semaphore(AI_CONCURRENCY)

    results = {}
    total_cost = 0.0
    for name, resolve_model in MODEL_COMPARISON_CANDIDATES.items():
        model_id = resolve_model()
        runs = await asyncio.gather(*[
            _one_run(item, checks, target_lang, source_lang, model_id, semaphore)
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
    lines = [f"Сравнение моделей для языка {report['target_lang']}:"]
    for name, r in report["results"].items():
        label = _NAMES_RU.get(name, name)
        lines.append(
            f"— {label}: поймала {r['catches']} из {r['runs']} прогонов "
            f"({int(r['hit_rate'] * 100)}%), стоимость ${r['cost_usd']:.4f}"
            + (" [ответ был обрезан хотя бы раз]" if r["truncated"] else "")
        )
    lines.append(f"Итого потрачено на весь тест: ${report['total_cost_usd']:.4f}")
    return "\n".join(lines)
