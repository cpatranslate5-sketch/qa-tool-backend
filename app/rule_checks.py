"""
Rule-based (non-AI) checks — 100% reliable, run as plain text comparison
rather than asking a model, which can occasionally miss a mismatch in a
long text. These catch exactly the things regex is good at: numbers not
matching between source/translation, and placeholders/tags getting lost
or corrupted in translation.
"""
import re

NUMBER_RE = re.compile(r"\d[\d.,]*\d|\d")
# Common placeholder styles: {name}, {{name}}, %s, %1$s, <tag>...</tag>, [tag]
PLACEHOLDER_RE = re.compile(r"\{\{?[^}]+\}?\}|%\d*\$?[sd]|<[^>]+>|\[[^\]]+\]")

# A comma used as a DECIMAL separator, e.g. "0,40" (kopecks/cents,
# Russian/Azerbaijani-style) — matched only when 1-2 digits follow the
# comma, so it's never confused with a comma used to GROUP thousands
# (e.g. "50,000"), which really is a different number if it doesn't match.
_DECIMAL_COMMA_RE = re.compile(r"^(\d+),(\d{1,2})$")


def _normalize_number(tok: str) -> str:
    """Normalizes cosmetic-only formatting differences that are *expected*
    to differ between source and a correctly localized translation, so
    they aren't flagged as a real numbers mismatch:
      - a decimal comma ("0,40") is unified with a decimal point ("0.40") —
        the source is usually English (period), while many of the agency's
        target languages correctly use a comma for the same value.
      - a leading zero on an otherwise-plain digit run ("03" for a
        zero-padded hour) is unified with its unpadded form ("3") — both
        are the same time, just padded differently.
    Real numeric differences (50 vs 500, 0.40 vs 0.04) are untouched and
    still compare as different."""
    m = _DECIMAL_COMMA_RE.match(tok)
    normalized = f"{m.group(1)}.{m.group(2)}" if m else tok
    if "." not in normalized and len(normalized) > 1:
        normalized = normalized.lstrip("0") or "0"
    return normalized


def _decompose_grouped(tok: str) -> list[str]:
    """A token with 2+ separators (','/'.' combined) is almost always a
    DATE written as one glued-together run — "22.09.2026" — rather than an
    ordinary decimal or a single thousands-grouping comma (those have at
    most one separator). Dates are exactly the case where the grouping
    itself is expected to change between languages: day/month/year can
    come in a different order, and the separator can be "." or "/" (a
    slash-separated date like "09/22/2026" never even reaches here as one
    token, since '/' isn't part of NUMBER_RE — it's already three separate
    atoms). So a dotted date is split into its individual digit groups and
    compared as a multiset, order and separator both ignored — only the
    actual digits have to survive translation, exactly as Александр asked
    for after "22.09.2026" (from a $0.40-style source date written
    "09/22/2026") was wrongly flagged against its own, correctly
    reordered/reformatted translation.
    A token with 0-1 separators (an ordinary decimal, or a single
    thousands-grouping comma like "50,000") is left as one atom via
    _normalize_number, so a genuinely different number is still caught."""
    if tok.count(".") + tok.count(",") < 2:
        return [_normalize_number(tok)]
    return [_normalize_number(part) for part in re.split(r"[.,]", tok) if part]


def _extract_numbers(text: str) -> list[str]:
    return NUMBER_RE.findall(text)


def _flatten_numbers(nums: list[str]) -> list[str]:
    out: list[str] = []
    for n in nums:
        out.extend(_decompose_grouped(n))
    return out


def _extract_placeholders(text: str) -> list[str]:
    return PLACEHOLDER_RE.findall(text)


def check_numbers(source: str, translation: str) -> list[dict]:
    src_nums = _extract_numbers(source)
    tr_nums = _extract_numbers(translation)
    findings = []
    if sorted(_flatten_numbers(src_nums)) != sorted(_flatten_numbers(tr_nums)):
        findings.append({
            "type": "numbers",
            "severity": "high",
            "message": f"Числа в исходнике и переводе не совпадают. Исходник: {src_nums or '—'}. Перевод: {tr_nums or '—'}.",
        })
    return findings


def check_placeholders(source: str, translation: str) -> list[dict]:
    src_ph = _extract_placeholders(source)
    tr_ph = _extract_placeholders(translation)
    findings = []
    if sorted(src_ph) != sorted(tr_ph):
        missing = [p for p in src_ph if p not in tr_ph]
        extra = [p for p in tr_ph if p not in src_ph]
        parts = []
        if missing:
            parts.append(f"пропущены в переводе: {missing}")
        if extra:
            parts.append(f"лишние в переводе: {extra}")
        findings.append({
            "type": "placeholders",
            "severity": "high",
            "message": "Плейсхолдеры/теги не совпадают — " + "; ".join(parts) + ".",
        })
    return findings


def check_max_length(translation: str, max_length: int | None) -> list[dict]:
    if not max_length:
        return []
    length = len(translation)
    if length > max_length:
        return [{
            "type": "max_length",
            "severity": "medium",
            "message": f"Перевод длиннее лимита: {length} символов при ограничении {max_length}.",
        }]
    return []


def check_missing(source: str, translation: str) -> list[dict]:
    if source.strip() and not translation.strip():
        return [{
            "type": "missing",
            "severity": "high",
            "message": "Перевод отсутствует.",
        }]
    return []


# Terminal punctuation we require to survive translation — the client's
# stated rule is specifically about a dropped period/exclamation mark, so we
# don't flag a source that ends in "?" or "…" here, only "." and "!".
TERMINAL_PUNCT = ".!"

# What counts as "the translation still ends with a mark" — not just the
# Latin ".!?…". Many of our target languages end sentences with their own
# script's stop: Bengali/Hindi/other Indic scripts use the danda ("।"/"॥"),
# CJK uses fullwidth stops ("。！？"), Arabic/Urdu use "۔", Armenian "։",
# Ethiopic "።", Myanmar "။", Khmer "។". A translation ending in any of these
# is complete — flagging it as "missing punctuation" was simply wrong.
TERMINAL_PUNCT_ACCEPTABLE = ".!?…" + "।॥" + "。！？" + "۔" + "։" + "።" + "။" + "។"

# Languages that conventionally don't end sentences with any terminal mark
# at all (Thai and Lao script don't use one) — nothing to require here.
NO_TERMINAL_PUNCT_LANGS = {"th", "lo"}


def check_punctuation(source: str, translation: str, lang_code: str = "") -> list[dict]:
    findings = []
    src = source.rstrip()
    tr = translation.rstrip()
    lang_base = lang_code.split("-")[0].lower() if lang_code else ""

    if lang_base not in NO_TERMINAL_PUNCT_LANGS and src and src[-1] in TERMINAL_PUNCT:
        if not tr or tr[-1] not in TERMINAL_PUNCT_ACCEPTABLE:
            findings.append({
                "type": "punctuation",
                "severity": "low",
                "message": f"В исходнике в конце стоит «{src[-1]}», а перевод не заканчивается знаком препинания.",
            })

    if "  " in translation:
        findings.append({
            "type": "punctuation",
            "severity": "low",
            "message": "В переводе есть двойной пробел.",
        })

    return findings


def run_rule_checks(
    source: str,
    translation: str,
    checks: list[str],
    max_length: int | None = None,
    lang_code: str = "",
) -> list[dict]:
    findings = []
    if not translation.strip():
        return check_missing(source, translation)
    if "numbers" in checks:
        findings += check_numbers(source, translation)
    if "placeholders" in checks:
        findings += check_placeholders(source, translation)
    if "max_length" in checks:
        findings += check_max_length(translation, max_length)
    if "punctuation" in checks:
        findings += check_punctuation(source, translation, lang_code)
    return findings
