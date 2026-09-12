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

from app.claude_client import (
    _model_for_lang,
    build_batch_prompt,
    create_message_batch,
    get_batch_results,
    get_batch_status,
    group_batch_findings,
    parse_json_array,
    run_ai_checks_batch,
)
from app.rule_checks import run_rule_checks

LANG_CODE_RE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,5})?$")
AI_CONCURRENCY = 5

# Above this much combined text (characters, summed across every checkable
# row × every target language — a rough proxy for total AI cost and how
# long a synchronous run would take), a multi-check is submitted through
# Anthropic's Message Batches API instead of run live: half the per-token
# price, but results typically land within an hour instead of a couple of
# minutes. A short single-language check, or a handful of languages, stays
# under this and runs instantly as before — but a small document checked
# across many languages (which is what actually drives the per-token cost
# up) will typically cross this threshold too. This is a starting guess,
# not a measured number — tune it based on real usage once there's a
# track record of actual costs and turnaround times.
BATCH_THRESHOLD_CHARS = 10_000

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


def _base_lang(code: str) -> str:
    """The base language subtag of a code — "ko" from "ko-KR", "es" from
    "es-mx", or the whole thing if it has no region part."""
    return code.strip().lower().split("-")[0]


def _subtags(code: str) -> set[str]:
    """Every hyphen-separated piece of a code, lowercased — {"kk", "kz"}
    for "kk-KZ". Used to match a bare code against either the language part
    OR the region part of a fuller one, since the agency's own documents
    are inconsistent about which they use for a short label — the
    Glossary/Tone docs label some languages by their country's code alone
    ("KZ" for Kazakh, "TJ" for Tajik, "BD" for Bengali) rather than the
    actual ISO language subtag ("kk", "tg", "bn") that Numerals uses in
    "kk-KZ"/"tg-TJ"/"bn-BD" — a plain prefix-only match would miss these
    entirely, since "kz" isn't the base of "kk-kz"."""
    return set(code.strip().lower().split("-"))


def _freeze(value):
    """Makes a value hashable/comparable for the equality check in
    resolve_lang_code below — a Numerals row is a dict of fields, a Tone
    row is a plain string."""
    if isinstance(value, dict):
        return tuple(sorted(value.items()))
    return value


def resolve_lang_code(requested: str, available, values: dict | None = None):
    """Matches a requested language code against a set/dict/iterable of
    codes actually present in one document, bridging granularity mismatches
    between the project's different reference documents — e.g. the
    Glossary may use a plain "ko" while the Numerals doc uses region-
    qualified "ko-KR" for the same language.

    Tries an exact (case-insensitive) match first. Failing that, falls
    back to matching the requested code against any subtag (language part
    OR region part — see _subtags) of an available code — but ONLY when
    exactly one available code contains it; if several do (e.g. Numerals
    has both "es-ES" and "es-AR" with genuinely different number formats),
    guessing would silently apply the wrong regional rule, so this returns
    None instead — the caller then treats the language as if it had no
    entry at all, exactly like today's "document uploaded but this
    language is missing" case, rather than picking one region at random.

    Some languages genuinely split into a handful of regional variants
    where every OTHER variant besides one or two special cases shares the
    same rule (e.g. Spanish: Spain and Argentina each have their own
    format, but "the rest of Latin America" is one shared format that can
    show up under any of several country codes — es-MX, es-CL, es-PE...).
    The agency already has a way to express that directly in the document
    itself: list every code that shares one row together in one cell,
    separated by "/" (already used for French: "fr-CI / fr-FR") — every
    code listed then resolves by exact match, no ambiguity at all. As a
    safety net for a code that WASN'T listed, if `values` (a {code: value}
    mapping) is passed, several same-base candidates still resolve when
    they all happen to carry the identical value — applying it is safe
    regardless of which one is picked, since they don't actually disagree.

    Returns the matching code from `available` (preserving its original
    casing), or None if nothing resolves safely.
    """
    requested = (requested or "").strip().lower()
    if not requested:
        return None

    avail_list = list(available)
    by_lower = {a.lower(): a for a in avail_list}
    if requested in by_lower:
        return by_lower[requested]

    requested_subtags = _subtags(requested)
    matches = [a for a in avail_list if requested_subtags & _subtags(a)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1 and values is not None:
        distinct = {_freeze(values[a]) for a in matches if a in values}
        if len(distinct) == 1:
            return matches[0]
    return None


def merge_lang_codes(codes) -> list[str]:
    """Deduplicates a set of language codes gathered from several documents
    for display (e.g. the target-language picker) — when a base language
    has only one region variant across everything ("ko" in one doc, "ko-KR"
    in another), it's shown once, as its more specific spelling. When a
    base language genuinely has several distinct region variants (es-ES,
    es-AR, es-MX), each is kept as its own separate entry, since they mean
    different formatting rules and must be picked explicitly.

    Region-qualified codes are grouped by their first (language) subtag
    only — deliberately narrower than resolve_lang_code's any-subtag
    match, since two region-qualified codes should never merge just for
    sharing a region (hi-IN and mr-IN are different languages that happen
    to both be spoken in India). A BARE code with no region of its own
    (like Tone's country-style "KZ" for Kazakh) is looser by nature — it's
    merged into whichever region-qualified group it matches on ANY subtag,
    but only when that's unambiguous (exactly one group matches)."""
    codes = [(c or "").strip() for c in codes if c and c.strip()]
    hyphenated = [c for c in codes if "-" in c]
    bare = [c for c in codes if "-" not in c]

    groups: dict[str, list[str]] = {}
    for code in hyphenated:
        groups.setdefault(_base_lang(code), []).append(code)

    for code in bare:
        low = code.lower()
        matching_bases = {
            base for base, variants in groups.items()
            if any(low in _subtags(v) for v in variants)
        }
        if len(matching_bases) == 1:
            groups[next(iter(matching_bases))].append(code)
        else:
            groups.setdefault(low, []).append(code)

    result = []
    for base, variants in groups.items():
        distinct = sorted(set(variants))
        regioned = [v for v in distinct if "-" in v]
        if len(regioned) >= 2:
            # Several genuinely different regional variants (es-ES vs
            # es-AR vs es-MX) — keep each; drop any bare/generic spelling
            # of the same base, since it's ambiguous which region it means.
            result.extend(regioned)
        elif len(regioned) == 1:
            # Only one spelling actually matters for this language — a
            # bare "ko" alongside it is the same language, not a second one.
            result.append(regioned[0])
        else:
            result.append(distinct[0])
    return sorted(result)


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
    numeral_rule: str = "",
    tone_register: str = "",
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
        ai_findings_by_idx = await run_ai_checks_batch(
            ai_items, glossary, checks, extra_instructions, numeral_rule, tone_register, lang, source_lang
        )

    out = []
    for idx, row in enumerate(relevant_rows):
        src = row["values"].get(source_lang, "")
        tgt = row["values"].get(lang, "")
        findings = run_rule_checks(src, tgt, checks, max_length=row["max_length"], lang_code=lang)
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
    numerals_for_lang=None,
    tone_for_lang=None,
    target_langs_filter: set[str] | None = None,
) -> dict:
    """
    glossary_for_lang / numerals_for_lang / tone_for_lang: each a
    callable(lang_code) -> prompt text (or "" if nothing for that language),
    already narrowed to just what this one target language needs — see
    app.glossary / app.project_docs. Each target language gets its own
    call, so the AI prompt for e.g. "es-mx" never carries the other 34
    languages' rows.

    target_langs_filter: when given, only these languages are checked even
    if the file has more columns — lets a manager check a subset of a
    large upload instead of every language every time.
    """
    numerals_for_lang = numerals_for_lang or (lambda lang: {})
    tone_for_lang = tone_for_lang or (lambda lang: "")
    semaphore = asyncio.Semaphore(AI_CONCURRENCY)
    result_sheets = []
    total_findings = 0
    total_rows_checked = 0

    for sheet in sheets:
        target_langs = [l for l in sheet["languages"] if l != source_lang]
        if target_langs_filter is not None:
            target_langs = [l for l in target_langs if l in target_langs_filter]
        tasks = [
            _check_language_for_sheet(
                sheet, lang, source_lang, glossary_for_lang(lang), checks, extra_instructions, semaphore,
                numerals_for_lang(lang), tone_for_lang(lang),
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


# ------------------------------------------------- large jobs: batch mode ---
# See BATCH_THRESHOLD_CHARS above. Instead of awaiting every language's AI
# call directly (run_multi_check), a large job is prepared as a "skeleton"
# (rule-based findings, computed instantly and for free) plus one Anthropic
# Message Batch request per language; once that batch finishes — polled from
# app.main — finalize_batch_results merges the AI findings back in to
# produce the exact same {"sheets": [...], "summary": {...}} shape as
# run_multi_check, so the frontend doesn't need to know which path ran.

def estimate_check_volume(
    sheets: list[dict], source_lang: str, target_langs_filter: set[str] | None = None
) -> int:
    """Rough proxy (total characters, summed across every checkable row ×
    every target language) for how expensive/slow a synchronous run would
    be. Not exact — a ballpark is all that's needed to pick a processing
    mode. Respects target_langs_filter so checking only a handful of a
    file's languages doesn't get pushed into the slow queue on the
    strength of languages that won't even be checked this run."""
    total = 0
    for sheet in sheets:
        target_langs = [l for l in sheet["languages"] if l != source_lang]
        if target_langs_filter is not None:
            target_langs = [l for l in target_langs if l in target_langs_filter]
        for row in sheet["rows"]:
            src = row["values"].get(source_lang, "")
            for lang in target_langs:
                tgt = row["values"].get(lang, "")
                if not src.strip() and not tgt.strip():
                    continue
                total += len(src) + len(tgt)
    return total


def build_batch_plan(
    sheets: list[dict],
    source_lang: str,
    glossary_for_lang,
    checks: list[str],
    extra_instructions: str = "",
    numerals_for_lang=None,
    tone_for_lang=None,
    target_langs_filter: set[str] | None = None,
) -> tuple[list[dict], dict]:
    """Prepares everything needed to submit one Anthropic Message Batch
    covering every (sheet, target language) pair in this upload, plus a
    JSON-serializable "skeleton" — already-computed rule-based findings —
    to merge the AI results into later via finalize_batch_results.

    Returns (batch_requests, skeleton). batch_requests is a list of
    {"custom_id": str, "prompt": str} ready for
    claude_client.create_message_batch; it can be empty if no AI check
    types were selected at all, in which case there's nothing to submit and
    finalize_batch_results(skeleton, {}) is already the final answer.
    """
    numerals_for_lang = numerals_for_lang or (lambda lang: {})
    tone_for_lang = tone_for_lang or (lambda lang: "")
    requests: list[dict] = []
    skeleton_sheets = []

    for s_idx, sheet in enumerate(sheets):
        target_langs = [l for l in sheet["languages"] if l != source_lang]
        if target_langs_filter is not None:
            target_langs = [l for l in target_langs if l in target_langs_filter]
        languages_skeleton = {}

        for lang in target_langs:
            relevant_rows = []
            ai_items = []
            for row in sheet["rows"]:
                src = row["values"].get(source_lang, "")
                tgt = row["values"].get(lang, "")
                if not src.strip() and not tgt.strip():
                    continue
                relevant_rows.append(row)
                ai_items.append({"context": row["context"], "source": src, "translation": tgt})

            # Rule-based findings are free and instant — compute them now
            # rather than waiting on the batch for them too.
            base_rows = []
            for row in relevant_rows:
                src = row["values"].get(source_lang, "")
                tgt = row["values"].get(lang, "")
                findings = run_rule_checks(src, tgt, checks, max_length=row["max_length"], lang_code=lang)
                base_rows.append({
                    "excel_row": row["excel_row"],
                    "context": row["context"],
                    "source": src,
                    "translation": tgt,
                    "findings": findings,
                })

            custom_id = f"s{s_idx}-{lang}"
            prompt, number_to_index = build_batch_prompt(
                ai_items, glossary_for_lang(lang), checks, extra_instructions,
                numerals_for_lang(lang), tone_for_lang(lang), lang, source_lang,
            )
            if prompt is not None:
                requests.append({"custom_id": custom_id, "prompt": prompt, "model": _model_for_lang(lang)})

            languages_skeleton[lang] = {
                "custom_id": custom_id if prompt is not None else None,
                "number_to_index": {str(k): v for k, v in number_to_index.items()},
                "rows": base_rows,
            }

        skeleton_sheets.append({
            "sheet_name": sheet["sheet_name"],
            "target_langs": target_langs,
            "languages": languages_skeleton,
            "unrecognized_columns": sheet.get("unrecognized_columns", []),
            "row_count": len(sheet["rows"]),
        })

    skeleton = {"sheets": skeleton_sheets, "source_lang": source_lang}
    return requests, skeleton


def finalize_batch_results(skeleton: dict, ai_text_by_custom_id: dict[str, str | None]) -> dict:
    """Merges AI findings (once the Anthropic batch has ended) into the
    rule-based skeleton from build_batch_plan, producing the same
    {"sheets": [...], "summary": {...}} shape run_multi_check returns."""
    result_sheets = []
    total_findings = 0
    total_rows_checked = 0

    for sheet in skeleton["sheets"]:
        languages_out = {}
        for lang, lang_skel in sheet["languages"].items():
            ai_grouped: dict[int, list[dict]] = {}
            custom_id = lang_skel["custom_id"]
            if custom_id is not None:
                text_block = ai_text_by_custom_id.get(custom_id)
                raw = parse_json_array(text_block)
                # JSON round-trips dict keys as strings — restore int keys.
                number_to_index = {int(k): v for k, v in lang_skel["number_to_index"].items()}
                ai_grouped = group_batch_findings(raw, number_to_index)

            findings_list = []
            for idx, row in enumerate(lang_skel["rows"]):
                findings = list(row["findings"]) + ai_grouped.get(idx, [])
                if findings:
                    findings_list.append({
                        "excel_row": row["excel_row"],
                        "context": row["context"],
                        "source": row["source"],
                        "translation": row["translation"],
                        "findings": findings,
                    })
            languages_out[lang] = findings_list
            total_findings += sum(len(f["findings"]) for f in findings_list)

        total_rows_checked += sheet["row_count"]
        result_sheets.append({
            "sheet_name": sheet["sheet_name"],
            "source_lang": skeleton["source_lang"],
            "languages_checked": sheet["target_langs"],
            "languages": languages_out,
            "unrecognized_columns": sheet["unrecognized_columns"],
        })

    summary = {
        "sheets": len(result_sheets),
        "rows_checked": total_rows_checked,
        "languages_checked": sorted({l for s in result_sheets for l in s["languages_checked"]}),
        "total_findings": total_findings,
    }
    return {"sheets": result_sheets, "summary": summary}


async def submit_multi_check_batch(requests: list[dict]) -> str | None:
    return await create_message_batch(requests)


async def try_finalize_batch(batch_id: str, skeleton: dict) -> dict | None:
    """Returns the finalized results dict once the Anthropic batch has
    ended, otherwise None (still processing — caller should try again
    later)."""
    status = await get_batch_status(batch_id)
    if status.get("processing_status") != "ended":
        return None
    results_url = status.get("results_url")
    ai_text_by_custom_id = await get_batch_results(results_url) if results_url else {}
    return finalize_batch_results(skeleton, ai_text_by_custom_id)


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
