"""
Parsing for the project's two smaller reference documents: Numerals
(number/currency/date/etc. format per language) and Tone-of-address
(formal/informal register per language).

Numerals is row-based — one row per language, several value columns (see
parse_numerals_workbook). Tone-of-address turned out, from the agency's
real file, to mirror the Glossary layout instead: language codes run
across the header row as columns, with the register sitting in the row(s)
below (see parse_tone_workbook) — not a simple row-per-language list as
originally assumed.

Each governs an AI check that refuses to run at all for a project with no
rows uploaded (see app.main._require_doc) — but a doc uploaded without a
row/column for some particular language just means that language's check
is skipped for that language, not blocked.
"""
import datetime
import io
import re

import openpyxl

from app.excel_multi import _normalize_lang_label

LANG_COL_NAMES = {"language", "lang", "язык", "код", "code"}

TONE_FORMAL_WORDS = ("формал", "вы", "formal")
TONE_INFORMAL_WORDS = ("неформал", "informal", "ты")

# Known Numerals-document column headers -> the Russian label used when
# building the rule description for the AI prompt (see
# app.claude_client._format_numeral_rule). Matched case-insensitively,
# trimmed. Any OTHER header present in the sheet is still kept (under its
# own raw header text as the label) rather than silently dropped — the
# agency may add more format columns later, and a new one should show up
# in the check rather than disappear until this map is updated.
NUMERAL_FIELD_LABELS = {
    "currency + >9999": "валюта при числах от 10 000",
    "currency + <10 000": "валюта при числах до 10 000",
    "iso code + >9999": "валюта ISO-кодом при числах от 10 000",
    "iso code + <10 000": "валюта ISO-кодом при числах до 10 000",
    "decimal": "разделитель дробной части",
    "date": "формат даты",
    "%": "формат процента",
    "100 000+": "формат чисел от 100 000",
    "1 000 000+": "формат чисел от 1 000 000",
    "multiplier <10 000": "формат умножения (например «x2500») при числах до 10 000",
    "multiplier >9999": "формат умножения при числах от 10 000",
    "time format": "формат времени",
}

_LANG_SPLIT_RE = re.compile(r"\s*/\s*")

# Matches the run of format-code letters Excel uses for a date/time cell's
# displayed appearance (e.g. "dd.mm.yyyy", "d/m/yy", "hh:mm AM/PM") — used to
# rebuild that literal display text from a parsed date/time value (see
# _excel_date_display, below).
_EXCEL_DATE_TOKEN_RE = re.compile(r"(yyyy|yy|dddd|ddd|dd|d|mmmm|mmm|mm|m|hh|h|ss|s|am/pm|a/p)", re.IGNORECASE)


def _excel_date_display(val, number_format: str) -> str:
    """openpyxl hands back a date/time cell's value as a plain Python
    date/datetime/time object — str() on that always renders it as ISO
    ("2023-08-16 00:00:00"), silently throwing away whatever separator and
    field order the sheet actually displays (e.g. "16.08.2023"). For the
    Numerals document that display text IS the rule (it's what the AI check
    is told the correct format looks like for this language), so this
    rebuilds it from the cell's own number_format instead of assuming any
    one convention (period vs slash, day-first vs month-first, etc.).

    Falls back to str(val) when number_format is missing, "General", or
    contains something this doesn't recognize — better to show the model
    *something* than crash the whole upload over one exotic format string.
    """
    fmt = (number_format or "").strip()
    if not fmt or fmt.lower() == "general":
        return str(val)

    is_time_only = isinstance(val, datetime.time) and not isinstance(val, datetime.datetime)

    def repl(match: re.Match) -> str:
        token = match.group(0)
        lower = token.lower()
        if lower in ("am/pm", "a/p"):
            return "%p"
        if lower.startswith("y"):
            return "%Y" if len(token) >= 4 else "%y"
        if lower.startswith("d"):
            return "%A" if len(token) >= 4 else ("%a" if len(token) == 3 else "%d")
        if lower.startswith("h"):
            return "%H"
        if lower.startswith("s"):
            return "%S"
        if lower.startswith("m"):
            # Excel reuses "m"/"mm" for MONTH in a date value but MINUTES in
            # a time-only value — it disambiguates by context, so we do too.
            return "%M" if is_time_only else "%m"
        return token

    try:
        py_format = _EXCEL_DATE_TOKEN_RE.sub(repl, fmt)
        return val.strftime(py_format)
    except Exception:
        return str(val)


def _find_lang_col(ws, header_row: int) -> int:
    """The language column is usually labeled something recognizable, but
    falls back to column 1 (matching the glossary parser's same forgiving
    approach — a blank/odd header shouldn't break the whole upload)."""
    for c in range(1, ws.max_column + 1):
        v = ws.cell(row=header_row, column=c).value
        label = v.strip().lower() if isinstance(v, str) else ""
        if label in LANG_COL_NAMES:
            return c
    return 1


def _numeral_field_columns(ws, header_row: int, lang_col: int) -> list[tuple[int, str]]:
    """Returns [(col_index, russian_label), ...] for every non-language,
    non-empty header column on the sheet — whatever format columns this
    particular export actually has, not a fixed assumed set."""
    cols = []
    for c in range(1, ws.max_column + 1):
        if c == lang_col:
            continue
        v = ws.cell(row=header_row, column=c).value
        label = v.strip() if isinstance(v, str) else ""
        if not label:
            continue
        ru_label = NUMERAL_FIELD_LABELS.get(label.lower(), label)
        cols.append((c, ru_label))
    return cols


def parse_numerals_workbook(file_bytes: bytes) -> list[dict]:
    """Returns [{"lang_code": "es-mx", "fields": {"<russian label>": "<value>", ...}}, ...].

    The real document (a Google Sheets export) has one language per row and
    around a dozen distinct format columns — currency (symbol and ISO code,
    for large and small numbers), decimal separator, date, percent, number
    grouping, multiplier and time format — rather than one free-text rule.
    Every column actually present in the header row is captured under its
    own label, so nothing is lost and nothing has to be hardcoded ahead of
    the agency adding more columns later.

    A workbook may have several sheets — e.g. one big master reference
    sheet plus smaller per-client-project subsets of it. Every sheet whose
    header row has recognizable format columns is parsed and merged; a
    later sheet's row for a language overrides an earlier one, so a
    project-specific sheet can refine or override the shared master. A
    language cell listing several codes together ("fr-CI / fr-FR") applies
    the same row to each of them.
    """
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    by_lang: dict[str, dict] = {}

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        if ws.max_row < 2:
            continue

        # Simple, fixed layout (unlike the Crowdin-style multi-check
        # export) — row 1 is always the header, no need to scan for it.
        header_row = 1
        lang_col = _find_lang_col(ws, header_row)
        field_cols = _numeral_field_columns(ws, header_row, lang_col)
        if not field_cols:
            continue

        for r in range(header_row + 1, ws.max_row + 1):
            lang_val = ws.cell(row=r, column=lang_col).value
            lang_label = str(lang_val).strip() if lang_val else ""
            if not lang_label:
                continue

            fields = {}
            for c, ru_label in field_cols:
                cell = ws.cell(row=r, column=c)
                val = cell.value
                if isinstance(val, (datetime.date, datetime.time)):
                    # A "Date"/"Time format" column example is usually typed
                    # as a real Excel date/time, not plain text — see
                    # _excel_date_display for why that needs special handling.
                    text = _excel_date_display(val, cell.number_format).strip()
                else:
                    text = str(val).strip() if val else ""
                if text:
                    fields[ru_label] = text
            if not fields:
                continue

            for one_label in _LANG_SPLIT_RE.split(lang_label):
                lang_code = _normalize_lang_label(one_label)
                if lang_code:
                    by_lang[lang_code] = fields

    return [{"lang_code": code, "fields": fields} for code, fields in by_lang.items()]


def _classify_tone(raw: str) -> str:
    low = raw.lower()
    if any(w in low for w in TONE_INFORMAL_WORDS):
        return "informal"
    if any(w in low for w in TONE_FORMAL_WORDS):
        return "formal"
    return ""


def parse_tone_workbook(file_bytes: bytes) -> list[dict]:
    """Returns [{"lang_code": "es-mx", "register": "formal"|"informal"}, ...].

    The real document mirrors the Glossary's layout, not a simple
    row-per-language list: language codes run across the header row as
    columns (same style, including the "ES (MX)" display variant and a
    "/"-separated combined header applying to several codes at once), and
    the register ("Формальное"/"Неформальное") sits in the row(s) below —
    normally just one data row, but every row under a language column is
    scanned and the first non-empty one wins, so a stray blank formatting
    row in the export doesn't break anything.
    """
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    by_lang: dict[str, str] = {}

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        if ws.max_row < 2:
            continue

        header_row = 1
        lang_cols: dict[int, list[str]] = {}
        for c in range(1, ws.max_column + 1):
            v = ws.cell(row=header_row, column=c).value
            label = v.strip() if isinstance(v, str) else ""
            if not label or label.lower() in LANG_COL_NAMES:
                continue
            codes = []
            for one_label in _LANG_SPLIT_RE.split(label):
                code = _normalize_lang_label(one_label)
                if code and " " not in code and len(code) <= 12:
                    codes.append(code)
            if codes:
                lang_cols[c] = codes
        if not lang_cols:
            continue

        for c, codes in lang_cols.items():
            register = ""
            for r in range(header_row + 1, ws.max_row + 1):
                val = ws.cell(row=r, column=c).value
                raw = str(val).strip() if val else ""
                if not raw:
                    continue
                register = _classify_tone(raw)
                if register:
                    break
            if not register:
                continue
            for code in codes:
                by_lang[code] = register

    return [{"lang_code": code, "register": register} for code, register in by_lang.items()]
