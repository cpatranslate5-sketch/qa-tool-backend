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


def _extract_numbers(text: str) -> list[str]:
    return NUMBER_RE.findall(text)


def _extract_placeholders(text: str) -> list[str]:
    return PLACEHOLDER_RE.findall(text)


def check_numbers(source: str, translation: str) -> list[dict]:
    src_nums = _extract_numbers(source)
    tr_nums = _extract_numbers(translation)
    findings = []
    if sorted(src_nums) != sorted(tr_nums):
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


def run_rule_checks(
    source: str,
    translation: str,
    checks: list[str],
    max_length: int | None = None,
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
    return findings
