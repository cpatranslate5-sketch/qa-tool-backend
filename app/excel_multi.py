"""
Parsing and checking of "Мульти" Excel uploads — the format Crowdin exports:
one sheet (sometimes several) with a header row of language codes, a
"Context" column naming each string, an optional "Max. length" column, and
one row per translatable string.
"""
import asyncio
import io
import re

import openpyxl

from app.claude_client import run_ai_checks_batch
from app.rule_checks import run_rule_checks

LANG_CODE_RE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,5})?$")
AI_CONCURRENCY = 5

# Known non-language metadata column names seen in Crowdin-style exports.
# Anything NOT in this list and not Context/Max length is treated as a
# language column — real client files have non-standard codes (a 4-letter
# code, a look-alike Cyrillic character typo'd into a code, etc.) that a
# strict regex would silently drop, which is worse than being permissive.
META_COL_NAMES = {
    "context", "key", "id", "string id", "identifier", "comment",
    "status", "screenshot", "reference", "notes", "note",
}


def _is_context_col(header: str) -> bool:
    return header.strip().lower() == "context"


def _is_max_length_col(header: str) -> bool:
    h = header.strip().lower()
    return "max" in h and "length" in h


def _is_meta_col(header: str) -> bool:
    return header.strip().lower() in META_COL_NAMES


_PAREN_LANG_RE = re.compile(r"^([a-zA-Zа-яА-Я]{2,3})\s*\(([a-zA-Zа-яА-Я0-9]{1,5})\)$")


def _normalize_lang_label(label: str) -> str:
    """Turns a display-style language header like "ES (MX)" or "PT (BR)"
    into the hyphenated form used everywhere else ("es-mx", "pt-br"), while
    leaving an already-plain code like "es-mx" or "fr-сi" untouched. Some
    client files use one style, some the other, so both need to resolve to
    the same lang_code."""
    label = label.strip()
    m = _PAREN_LANG_RE.match(label)
    if m:
        return f"{m.group(1).lower()}-{m.group(2).lower()}"
    return label.lower()


def _find_header_row(ws, max_scan: int = 5) -> int:
    best_row, best_score = 1, -1
    for r in range(1, min(max_scan, ws.max_row) + 1):
        score = 0
        for c in range(1, ws.max_column + 1):
            v = ws.cell(row=r, column=c).value
            if isinstance(v, str) and LANG_CODE_RE.match(v.strip().lower()):
                score += 1
        if score > best_score:
            best_row, best_score = r, score
    return best_row


def parse_workbook(file_bytes: bytes) -> list[dict]:
    """Returns a list of parsed sheets: each with lang codes found and rows."""
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    sheets = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        if ws.max_row < 2:
            continue

        header_row = _find_header_row(ws)
        context_col = max_length_col = None
        lang_cols: dict[int, str] = {}
        unrecognized: list[str] = []

        for c in range(1, ws.max_column + 1):
            v = ws.cell(row=header_row, column=c).value
            if not isinstance(v, str) or not v.strip():
                continue
            label = v.strip()
            if _is_context_col(label):
                context_col = c
            elif _is_max_length_col(label):
                max_length_col = c
            elif _is_meta_col(label):
                continue
            else:
                normalized = _normalize_lang_label(label)
                if " " in normalized or len(normalized) > 12:
                    # Looks like prose, not a language code — skip rather
                    # than misread a stray comment column as a "language".
                    unrecognized.append(label)
                else:
                    lang_cols[c] = normalized

        if not lang_cols:
            continue

        rows = []
        last_context = ""
        for r in range(header_row + 1, ws.max_row + 1):
            values = {code: ws.cell(row=r, column=c).value for c, code in lang_cols.items()}
            if all(v is None or str(v).strip() == "" for v in values.values()):
                continue

            context = ""
            if context_col:
                raw_ctx = ws.cell(row=r, column=context_col).value
                context = str(raw_ctx).strip() if raw_ctx else ""
            if context:
                last_context = context
            else:
                context = last_context

            max_length = None
            if max_length_col:
                raw_ml = ws.cell(row=r, column=max_length_col).value
                if isinstance(raw_ml, (int, float)):
                    max_length = int(raw_ml)

            rows.append({
                "excel_row": r,
                "context": context,
                "max_length": max_length,
                "values": {code: ("" if v is None else str(v)) for code, v in values.items()},
            })

        sheets.append({
            "sheet_name": sheet_name,
            "languages": sorted(lang_cols.values()),
            "rows": rows,
            "unrecognized_columns": unrecognized,
        })

    return sheets


def pick_source_lang(sheets: list[dict], preferred: str | None) -> str:
    all_langs: set[str] = set()
    for s in sheets:
        all_langs.update(s["languages"])
    if preferred and preferred in all_langs:
        return preferred
    if "en" in all_langs:
        return "en"
    return sorted(all_langs)[0] if all_langs else "en"


async def _check_language_for_sheet(
    sheet: dict,
    lang: str,
    source_lang: str,
    glossary: str,
    checks: list[str],
    extra_instructions: str,
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    relevant_rows = []
    ai_items = []
    for row in sheet["rows"]:
        src = row["values"].get(source_lang, "")
        tgt = row["values"].get(lang, "")
        if not src.strip() and not tgt.strip():
            continue
        relevant_rows.append(row)
        ai_items.append({"context": row["context"], "source": src, "translation": tgt})

    if not relevant_rows:
        return []

    async with semaphore:
        ai_findings_by_idx = await run_ai_checks_batch(ai_items, glossary, checks, extra_instructions)

    out = []
    for idx, row in enumerate(relevant_rows):
        src = row["values"].get(source_lang, "")
        tgt = row["values"].get(lang, "")
        findings = run_rule_checks(src, tgt, checks, max_length=row["max_length"])
        findings += ai_findings_by_idx.get(idx, [])
        if findings:
            out.append({
                "excel_row": row["excel_row"],
                "context": row["context"],
                "source": src,
                "translation": tgt,
                "findings": findings,
            })
    return out


async def run_multi_check(
    sheets: list[dict],
    source_lang: str,
    glossary_for_lang,
    checks: list[str],
    extra_instructions: str = "",
) -> dict:
    """
    glossary_for_lang: a callable(lang_code) -> glossary prompt text, already
    narrowed to EN + RU + that one target language — see app.glossary. Each
    target language gets its own call, so the AI prompt for e.g. "es-mx"
    never carries the other 34 languages' glossary rows.
    """
    semaphore = asyncio.Semaphore(AI_CONCURRENCY)
    result_sheets = []
    total_findings = 0
    total_rows_checked = 0

    for sheet in sheets:
        target_langs = [l for l in sheet["languages"] if l != source_lang]
        tasks = [
            _check_language_for_sheet(
                sheet, lang, source_lang, glossary_for_lang(lang), checks, extra_instructions, semaphore
            )
            for lang in target_langs
        ]
        per_lang_results = await asyncio.gather(*tasks) if tasks else []

        languages_out = {}
        for lang, findings_list in zip(target_langs, per_lang_results):
            languages_out[lang] = findings_list
            total_findings += sum(len(f["findings"]) for f in findings_list)

        total_rows_checked += len(sheet["rows"])
        result_sheets.append({
            "sheet_name": sheet["sheet_name"],
            "source_lang": source_lang,
            "languages_checked": target_langs,
            "languages": languages_out,
            "unrecognized_columns": sheet.get("unrecognized_columns", []),
        })

    summary = {
        "sheets": len(result_sheets),
        "rows_checked": total_rows_checked,
        "languages_checked": sorted({l for s in result_sheets for l in s["languages_checked"]}),
        "total_findings": total_findings,
    }
    return {"sheets": result_sheets, "summary": summary}


def build_report_workbook(filename: str, source_lang: str, results: dict) -> bytes:
    """Builds a downloadable .xlsx with one row per finding."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "QA Findings"
    ws.append(["Лист", "Строка в файле", "Контекст", "Язык", "Серьёзность", "Тип", "Проблема", "Источник", "Перевод"])
    for col_idx, width in enumerate([18, 14, 28, 8, 12, 14, 50, 40, 40], start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(col_idx)].width = width

    for sheet in results.get("sheets", []):
        for lang, findings_list in sheet.get("languages", {}).items():
            for item in findings_list:
                for f in item["findings"]:
                    ws.append([
                        sheet["sheet_name"],
                        item["excel_row"],
                        item["context"],
                        lang,
                        f.get("severity", ""),
                        f.get("type", ""),
                        f.get("message", ""),
                        item["source"],
                        item["translation"],
                    ])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
