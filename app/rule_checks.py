"""
Rule-based (non-AI) checks — 100% reliable, run as plain text comparison
rather than asking a model, which can occasionally miss a mismatch in a
long text. These catch exactly the things regex is good at: numbers not
matching between source/translation, and placeholders/tags getting lost
or corrupted in translation.
"""
import re
from collections import Counter

NUMBER_RE = re.compile(r"\d[\d.,]*\d|\d")
# Common placeholder styles: {name}, {{name}}, %s, %1$s, <tag>...</tag>, [tag]
PLACEHOLDER_RE = re.compile(r"\{\{?[^}]+\}?\}|%\d*\$?[sd]|<[^>]+>|\[[^\]]+\]")

# A comma used as a DECIMAL separator, e.g. "0,40" (kopecks/cents,
# Russian/Azerbaijani-style) — matched only when 1-2 digits follow the
# comma, so it's never confused with a comma used to GROUP thousands
# (e.g. "50,000"), which really is a different number if it doesn't match.
_DECIMAL_COMMA_RE = re.compile(r"^(\d+),(\d{1,2})$")

# A comma used purely to GROUP thousands, e.g. "1,400", "1,500,000" — one or
# more commas, each followed by exactly 3 digits. Distinct from the decimal
# comma above by that 3-digit group size (a decimal comma is always 1-2
# digits), so the two patterns never collide. The grouping itself is pure
# formatting: some target languages/translators drop it (or space it, which
# NUMBER_RE never captures as part of the number to begin with), so "1,400"
# and "1400" are the same value and must compare equal.
_THOUSANDS_GROUPED_RE = re.compile(r"^\d{1,3}(?:,\d{3})+$")

# A plain space (regular, non-breaking, or thin) is ALSO a standard
# thousands separator — it's how Russian formats a big number ("1 500 000"),
# while the same value shows up comma-grouped in English/Spanish
# ("1,500,000"). NUMBER_RE can't include a bare space in its own character
# class (that would merge any two unrelated numbers separated by ordinary
# whitespace, e.g. "5 победителей" + "9 призов"), so this instead matches
# the shape directly in the source text — a 1-3 digit group followed by one
# or more further EXACTLY-3-digit groups, each separated by a single space
# with nothing else between them — before numbers are extracted at all, and
# joins each match into one plain digit run first. Александр's real promo
# files write the exact same prize amounts space-grouped in Russian and
# comma-grouped in English/Spanish side by side; without this, literally
# every big number in a prize table came back "different" for no reason.
_SPACE_THOUSANDS_RE = re.compile(r"\d{1,3}(?:[   ]\d{3})+")


def _merge_space_thousands(text: str) -> str:
    return _SPACE_THOUSANDS_RE.sub(lambda m: re.sub(r"[   ]", "", m.group(0)), text)


def _normalize_number(tok: str) -> str:
    """Normalizes cosmetic-only formatting differences that are *expected*
    to differ between source and a correctly localized translation, so
    they aren't flagged as a real numbers mismatch:
      - a thousands-grouping comma ("1,400", "1,500,000") is removed
        entirely, since keeping or dropping it doesn't change the value —
        Александр hit a translation that correctly kept some of a promo's
        numbers grouped ("1,500,000") but wrote smaller ones ungrouped
        ("1400" for the source's "1,400"), and it was flagged as a mismatch
        even though the value never changed.
      - a decimal comma ("0,40") is unified with a decimal point ("0.40") —
        the source is usually English (period), while many of the agency's
        target languages correctly use a comma for the same value.
      - a leading zero on an otherwise-plain digit run ("03" for a
        zero-padded hour) is unified with its unpadded form ("3") — both
        are the same time, just padded differently.
    Real numeric differences (50 vs 500, 0.40 vs 0.04, 1,400 vs 1,500) are
    untouched and still compare as different."""
    if _THOUSANDS_GROUPED_RE.match(tok):
        return tok.replace(",", "")
    m = _DECIMAL_COMMA_RE.match(tok)
    normalized = f"{m.group(1)}.{m.group(2)}" if m else tok
    if "." not in normalized and len(normalized) > 1:
        normalized = normalized.lstrip("0") or "0"
    return normalized


def _decompose_grouped(tok: str) -> list[str]:
    """A token that's unambiguously a comma-grouped THOUSANDS number
    ("1,400", "1,500,000") is one single value — _normalize_number above
    already merges its commas away, so it stays one atom. What's left with
    2+ separators is a DATE written as one glued-together run —
    "22.09.2026" — since an ordinary decimal or a recognized thousands
    grouping never reaches this branch. Dates are exactly the case where
    the grouping itself is expected to change between languages:
    day/month/year can come in a different order, and the separator can be
    "." or "/" (a slash-separated date like "09/22/2026" never even
    reaches here as one token, since '/' isn't part of NUMBER_RE — it's
    already three separate atoms). So a dotted date is split into its
    individual digit groups and compared as a multiset, order and
    separator both ignored — only the actual digits have to survive
    translation, exactly as Александр asked for after "22.09.2026" (from a
    $0.40-style source date written "09/22/2026") was wrongly flagged
    against its own, correctly reordered/reformatted translation.
    A token with 0-1 separators (an ordinary decimal, or a single
    thousands-grouping comma like "50,000") is left as one atom via
    _normalize_number, so a genuinely different number is still caught."""
    normalized = _normalize_number(tok)
    if normalized != tok or (tok.count(".") + tok.count(",")) < 2:
        return [normalized]
    return [_normalize_number(part) for part in re.split(r"[.,]", tok) if part]


def _extract_numbers(text: str) -> list[str]:
    return NUMBER_RE.findall(_merge_space_thousands(text))


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
    src_flat = _flatten_numbers(src_nums)
    tr_flat = _flatten_numbers(tr_nums)
    findings = []
    if sorted(src_flat) != sorted(tr_flat):
        # Point at the SPECIFIC number(s) that actually differ, not a dump
        # of every number in the text — a long promo paragraph can easily
        # have 20-30 numbers where only one is actually wrong, and the raw
        # full lists made that one real difference hard to spot by eye
        # (Александр kept asking "why is it showing me this" at a glance).
        src_counter = Counter(src_flat)
        tr_counter = Counter(tr_flat)
        missing = sorted((src_counter - tr_counter).elements())
        extra = sorted((tr_counter - src_counter).elements())
        parts = []
        if missing:
            parts.append(f"есть в исходнике, нет в переводе: {missing}")
        if extra:
            parts.append(f"есть в переводе, нет в исходнике: {extra}")
        findings.append({
            "type": "numbers",
            "severity": "high",
            "message": "Числа в исходнике и переводе не совпадают — " + "; ".join(parts) + ".",
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
