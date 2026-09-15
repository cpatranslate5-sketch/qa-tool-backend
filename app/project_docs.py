"""
Parsing for the project's Tone-of-address reference document
(formal/informal register per language) — the only reference document
this app still has.

Language codes run across the header row as columns, with the register
sitting in the row(s) below (see parse_tone_workbook).

Governs an AI check that refuses to run at all for a project with no
rows uploaded (see app.main._require_doc) — but a doc uploaded without a
column for some particular language just means that language's check is
skipped for that language, not blocked.

(Two other documents used to live alongside this one but were removed:
Numerals — number/currency/date format per language — and Glossary —
required term-by-term translations. Both AI checks built on them kept
getting things wrong in ways that weren't worth patching further, so
both features were dropped rather than fixed again.)
"""
import io
import re

import openpyxl

from app.excel_multi import _label_to_code

LANG_COL_NAMES = {"language", "lang", "язык", "код", "code"}

TONE_FORMAL_WORDS = ("формал", "вы", "formal")
TONE_INFORMAL_WORDS = ("неформал", "informal", "ты")

_LANG_SPLIT_RE = re.compile(r"\s*/\s*")


def _classify_tone(raw: str) -> str:
    low = raw.lower()
    if any(w in low for w in TONE_INFORMAL_WORDS):
        return "informal"
    if any(w in low for w in TONE_FORMAL_WORDS):
        return "formal"
    return ""


def parse_tone_workbook(file_bytes: bytes, alias_map: dict[str, str] | None = None) -> list[dict]:
    """Returns [{"lang_code": "es-mx", "register": "formal"|"informal"}, ...].

    Not a simple row-per-language list: language codes run across the
    header row as columns (including the "ES (MX)" display variant and a
    "/"-separated combined header applying to several codes at once), and
    the register ("Формальное"/"Неформальное") sits in the row(s) below —
    normally just one data row, but every row under a language column is
    scanned and the first non-empty one wins, so a stray blank formatting
    row in the export doesn't break anything.

    alias_map: same manager-built global spelling dictionary used by
    app.excel_multi.parse_workbook (see _label_to_code) — applied here
    too so a code taught once means the same thing in every document,
    not just in files sent for checking.
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
                code = _label_to_code(one_label, alias_map)
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
