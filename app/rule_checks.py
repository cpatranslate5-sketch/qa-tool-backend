"""
Rule-based (non-AI) checks — 100% reliable, run as plain text comparison
rather than asking a model, which can occasionally miss a mismatch in a
long text. These catch exactly the things regex is good at: numbers not
matching between source/translation, and placeholders/tags getting lost
or corrupted in translation.
"""
import bisect
import re
from collections import Counter

NUMBER_RE = re.compile(r"\d[\d.,]*\d|\d")
# Common placeholder styles: {name}, {{name}}, %s, %1$s, <tag>...</tag>,
# [tag], \uXXXX (a literal escaped-unicode token, e.g. " " for a
# non-breaking space — some of Александр's Crowdin exports write it out
# literally as this six-character escape sequence rather than embedding
# the actual invisible character, so it needs its own pattern: none of the
# other styles above match a bare backslash+u+4-hex-digits run at all,
# which is exactly why it went unrecognized — "Earn points in tournament
# games and win cash prizes" lost that " " in translation
# without check_placeholders ever noticing).
PLACEHOLDER_RE = re.compile(r"\{\{?[^}]+\}?\}|%\d*\$?[sd]|<[^>]+>|\[[^\]]+\]|\\u[0-9a-fA-F]{4}")

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

# A period used the same way, e.g. "50.000", "1.500.000" — the mirror image
# of the comma pattern above: German, Spanish and several other of
# Александр's target languages group thousands with a period instead of a
# comma, so source "50000" and translation "50.000" are the same value, not
# a mismatch (this is exactly the false positive he hit: "50000"/"50.000"
# flagged as different numbers). Same exactly-3-digit-group shape as the
# comma version, so it carries the same accepted ambiguity — a genuine
# 3-decimal-place fraction like "0.125" would also match this shape and get
# treated as grouped — but that's already the tradeoff the comma rule above
# makes, and 3-decimal fractions don't come up in promo prize
# amounts/counts/percentages.
_DOT_THOUSANDS_GROUPED_RE = re.compile(r"^\d{1,3}(?:\.\d{3})+$")

# The Indian numbering system (lakh/crore) groups thousands differently —
# ONE group of 3 digits at the end, then every group before that is only 2
# digits: 100000 is "1,00,000" (not "100,000"), 1500000 is "15,00,000",
# 10000000 ("1 crore") is "1,00,00,000". Александр's concrete case
# (2026-09-17): a rupee amount written "₹1,00,000" in an Indian-language
# target column for the same value as "₹100,000" in the English source —
# correctly localized, but check_numbers had no idea "1,00,000" was even
# valid thousands grouping at all (it doesn't match the plain Western
# comma-every-3-digits shape above), so it fell through to being treated
# as a literal, un-normalized string and compared unequal to "100000".
# This shape is distinctive enough (a leading 1-2 digit group, then only
# 2-digit comma groups, ending in one final 3-digit group) that it's safe
# to recognize unconditionally — it never collides with a genuine Western
# thousands grouping or a date (dates use "." or "/", never ",").
_INDIAN_THOUSANDS_GROUPED_RE = re.compile(r"^\d{1,2}(?:,\d{2})+,\d{3}$")

# A short date written with a single "." and no year at all — "20.09"
# (day.month) or "09.20" (month.day) — Александр's other concrete case
# (2026-09-17): the existing multiset date comparison below only kicked in
# for a date with 2+ separators (a full day.month.year), because a plain
# 1-separator token was assumed to be an ordinary decimal number ("0.40")
# and had to stay a single atom for THAT case to compare correctly. A
# slash- or hyphen-separated short date ("09/20", "20-09") never had this
# problem to begin with — "/" and "-" aren't part of NUMBER_RE's own
# character class, so they're already split into separate digit tokens by
# _flatten_number_matches before any of this runs. Only the dot-joined
# form needed a fix. Gated on BOTH parts looking like plausible day/month
# digits (1-31) AND (see _near_currency_marker below) not sitting next to
# a currency symbol/code — a real decimal price ("45.67", or "$20.09")
# has every reason to have both halves in that range, so this stays safe
# for ordinary currency amounts
# values while fixing exactly the day/month-swap case Александр described.
def _looks_like_short_date(tok: str) -> bool:
    parts = tok.split(".")
    return len(parts) == 2 and all(p.isdigit() and 1 <= int(p) <= 31 for p in parts)


# The short-date fix above is genuinely ambiguous on the token alone: a
# real 2-decimal-place price ("$20.09", "$9.20", "$10.15" — completely
# ordinary in these casino/promo documents) has EXACTLY the same shape as
# a day.month date, and reviewing this live confirmed the risk was real:
# without this guard, check_numbers("Bonus: $20.09", "Bonus: $9.20")
# incorrectly returned no findings at all — a genuine transposition typo
# silently passing. Disambiguated by CONTEXT instead of shape: a date
# essentially never sits directly next to a currency marker, while a price
# almost always does — so the short-date decomposition is suppressed only
# when a currency symbol or code is close enough to plausibly belong to
# this exact number, leaving it as one exact-comparison atom (the same,
# safe, pre-fix behavior) precisely where it matters most.
_CURRENCY_SYMBOL_RE = re.compile(r"[$€£¥₹₼₴₸₩₪₫฿₽₦₱]")
_CURRENCY_CODE_RE = re.compile(r"\b(?:USD|EUR|RUB|USDT|GBP|INR|AZN|TRY|UZS|KZT|KGS|TJS|BRL|MXN|KRW)\b", re.IGNORECASE)
_CURRENCY_CONTEXT_WINDOW = 6


def _near_currency_marker(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - _CURRENCY_CONTEXT_WINDOW):start]
    after = text[end:end + _CURRENCY_CONTEXT_WINDOW]
    return bool(
        _CURRENCY_SYMBOL_RE.search(before) or _CURRENCY_SYMBOL_RE.search(after)
        or _CURRENCY_CODE_RE.search(before) or _CURRENCY_CODE_RE.search(after)
    )


# A second real collision with the short-date shape, caught live on
# Александр's actual production content (2026-09-22, a real betting-odds
# promo cell): odds are routinely written as a min-max RANGE of two
# decimals ("odds from 1.25 to 4.0" / "Quoten von 1,25 bis 4,0") — and
# "1.25" alone has exactly the same day.month shape as a real short date
# (both halves are 1-31), so it was being silently split into '1' and
# '25' and compared that way, while the correctly-localized translation
# ("1,25", normalized to "1.25") stayed one atom — a spurious mismatch on
# every single odds range in every language, in a business whose content
# is overwhelmingly gambling/casino promos full of exactly this pattern.
# Same fix strategy as the currency-marker case above: disambiguate by
# CONTEXT, not shape — a genuine date is essentially never paired with a
# SECOND decimal-shaped number nearby, while a range always is. Checked
# generically (any nearby "\d+\.\d+", not a specific connector word)
# since "to"/"bis"/"至"/"до"/... varies by language and this same check
# runs across every target language in the file.
#
# First version of this fix used a flat character-window (15 chars) —
# subagent review caught two real problems with that: (1) a translation
# that runs noticeably longer than the English source (German/Russian
# routinely do) can push the actual range partner outside a fixed small
# window, silently reopening the exact bug this was meant to fix; (2) an
# unrelated decimal a few characters from a genuine date (a stray
# multiplier right before a date in the same clause) could wrongly
# suppress that date's legitimate reorder-tolerant comparison. Scoping
# the search to the current SENTENCE instead of a fixed window fixes (1)
# outright — the partner is found no matter how verbose the phrasing, as
# long as it's still part of the same sentence — and narrows (2): only a
# genuinely same-sentence collision can still misfire, which is a much
# rarer, more defensible edge case than "anything within N characters".
# Reuses the same sentence-boundary regex and bisect-based scoping
# already established and tested for check_hyphen_for_dash below, for
# consistency and because that boundary definition (a real
# ./!/?/ellipsis, deliberately NOT a bare newline — see the notes above
# _SENTENCE_BOUNDARY_RE) is exactly the right granularity here too. A
# generous absolute cap keeps this bounded on pathological content with
# no real sentence punctuation at all (e.g. one giant unpunctuated cell).
#
# Matches EITHER a period or a comma as the decimal separator (like
# NUMBER_RE itself does) — not just a period. A period-only pattern here
# would only ever recognize an English-style "1.25" partner and silently
# miss a German/Russian-style "1,25" one, so a comma-decimal translation's
# own genuine date could wrongly be treated as having no partner nearby
# (and get split) while the period-decimal source correctly detects its
# partner (and doesn't) — a real asymmetry caught while stress-testing
# this exact fix against a source/translation pair mixing both styles.
#
# A THIRD real gap, caught by a second independent review pass: the other
# bound of a range is very often written as a plain WHOLE number with no
# decimal point at all ("odds from 1.25 to 4", not "...to 4.0") — normal,
# common phrasing. _DECIMAL_TOKEN_RE alone never matches that bound, so
# "1.25" was judged to have no partner and got split into ['1','25'] on
# the (period-decimal) source, while a comma-decimal translation's own
# "1,25" — which never even reaches this logic; see _decompose_grouped's
# early-return for a normalization-changed token — stayed one atom
# regardless of context, reproducing the exact original bug via that
# asymmetry. A bare integer is a much weaker signal than a second decimal
# (ordinary text is full of incidental integers — quantities, years, day
# counts — that have nothing to do with the candidate number, especially
# once the search reaches sentence-wide via the window above), so it's
# only accepted as a partner within a MUCH tighter window: a genuine
# range bound written as a bare integer is essentially always immediately
# adjacent, separated only by a short connector ("to 4", "-4", "до 4");
# an incidental unrelated integer landing that close is a much rarer
# coincidence than landing somewhere within the whole sentence.
#
# Excludes a colon-adjacent digit run ("23" in "23:59") on top of the
# obvious "already part of a bigger number" exclusion — a pre-existing,
# already-tested case ("Confirm by 09/20, 23:59" / "Подтверди до 20.09,
# 23:59") pairs a short date directly with a clock time one comma away,
# well within this tight window, and a bare time component is exactly
# the kind of coincidentally-nearby integer this heuristic must NOT treat
# as a range bound.
_DECIMAL_TOKEN_RE = re.compile(r"\d+[.,]\d+")
_INTEGER_TOKEN_RE = re.compile(r"(?<![\d.,:])\d+(?![\d.,:])")
_RANGE_PARTNER_WINDOW = 200
_RANGE_PARTNER_TIGHT_WINDOW = 15


def _near_range_partner(text: str, start: int, end: int, boundary_ends: list) -> bool:
    sent_start_idx = bisect.bisect_right(boundary_ends, start) - 1
    sent_start = boundary_ends[sent_start_idx] if sent_start_idx >= 0 else 0
    sent_end_idx = bisect.bisect_right(boundary_ends, end)
    sent_end = boundary_ends[sent_end_idx] if sent_end_idx < len(boundary_ends) else len(text)

    wide_start = max(sent_start, start - _RANGE_PARTNER_WINDOW)
    wide_end = min(sent_end, end + _RANGE_PARTNER_WINDOW)
    if _DECIMAL_TOKEN_RE.search(text, wide_start, start) or _DECIMAL_TOKEN_RE.search(text, end, wide_end):
        return True

    tight_start = max(sent_start, start - _RANGE_PARTNER_TIGHT_WINDOW)
    tight_end = min(sent_end, end + _RANGE_PARTNER_TIGHT_WINDOW)
    return bool(
        _INTEGER_TOKEN_RE.search(text, tight_start, start)
        or _INTEGER_TOKEN_RE.search(text, end, tight_end)
    )

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
      - a thousands-grouping comma OR period ("1,400"/"1.400",
        "1,500,000"/"1.500.000") is removed entirely, since keeping,
        dropping, or switching which punctuation mark groups the number
        doesn't change the value — Александр hit both directions of this:
        a translation that correctly kept some of a promo's numbers
        grouped ("1,500,000") but wrote smaller ones ungrouped ("1400" for
        the source's "1,400"), and a target language that grouped with a
        period instead of a comma ("50.000" for the source's "50000").
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
    if _DOT_THOUSANDS_GROUPED_RE.match(tok):
        return tok.replace(".", "")
    if _INDIAN_THOUSANDS_GROUPED_RE.match(tok):
        return tok.replace(",", "")
    m = _DECIMAL_COMMA_RE.match(tok)
    normalized = f"{m.group(1)}.{m.group(2)}" if m else tok
    if "." not in normalized and len(normalized) > 1:
        normalized = normalized.lstrip("0") or "0"
    return normalized


def _decompose_grouped(tok: str, allow_short_date: bool = True) -> list[str]:
    """A token that's unambiguously a comma- or period-grouped THOUSANDS
    number ("1,400", "1.400", "1,500,000", "1.500.000") is one single value
    — _normalize_number above already merges its separators away, so it
    stays one atom. What's left with 2+ separators is a DATE written as one
    glued-together run — "22.09.2026" — since an ordinary decimal or a
    recognized thousands grouping never reaches this branch (a dotted
    thousands number like "50.000" is already resolved to one atom by
    _normalize_number before this function's date-vs-grouping decision
    even runs, precisely so it's never mistaken for a 2-part date
    fragment). Dates are exactly the case where
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
    _normalize_number, so a genuinely different number is still caught —
    EXCEPT a 1-dot, both-halves-look-like-a-day-or-month token ("20.09",
    "09.20") that ISN'T sitting next to a currency marker, which is exactly
    as order-sensitive a false positive as the 2+-separator case above
    (Александр's second date example, 2026-09-17: a short day.month date
    with no year at all, "20.09" vs "09/20" between languages) — see
    _looks_like_short_date's and _near_currency_marker's own comments for
    why an ordinary price is kept safely out of this."""
    normalized = _normalize_number(tok)
    if normalized != tok:
        return [normalized]
    sep_count = tok.count(".") + tok.count(",")
    if sep_count >= 2:
        return [_normalize_number(part) for part in re.split(r"[.,]", tok) if part]
    if allow_short_date and _looks_like_short_date(tok):
        return [_normalize_number(part) for part in re.split(r"[.,]", tok) if part]
    return [normalized]


def _flatten_number_matches(text: str) -> list[str]:
    merged = _merge_space_thousands(text)
    # Sentence-boundary positions, computed once per call and reused for every
    # number in this text — see _near_range_partner's comment for why this
    # (not a flat character window) is what actually scopes "nearby" here.
    boundary_ends = [m.end() for m in _SENTENCE_BOUNDARY_RE.finditer(merged)]
    out: list[str] = []
    for m in NUMBER_RE.finditer(merged):
        tok = m.group(0)
        allow_short_date = not (
            _near_currency_marker(merged, m.start(), m.end())
            or _near_range_partner(merged, m.start(), m.end(), boundary_ends)
        )
        out.extend(_decompose_grouped(tok, allow_short_date=allow_short_date))
    return out


def _extract_placeholders(text: str) -> list[str]:
    return PLACEHOLDER_RE.findall(text)


def check_numbers(source: str, translation: str) -> list[dict]:
    src_flat = _flatten_number_matches(source)
    tr_flat = _flatten_number_matches(translation)
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


# Letter-run placeholder protection — Александр's ask (2026-09-17): some
# documents use a run of the SAME letter repeated several times as a
# stand-in for masked/dynamic data (a card number shown as "XXXXXXXX", a
# year format as "YYYY"). Two things must survive translation exactly:
# (1) the number of letters in the run — one added or dropped letter
# quietly breaks whatever format the placeholder represents — and
# (2) which ALPHABET it's written in — a Cyrillic "Х" looks pixel-for-
# pixel identical to a Latin "X" (the same invisible-swap problem
# check_mixed_script above already catches inside ordinary words), so a
# translator's Cyrillic keyboard slipping in a look-alike character is
# impossible to spot by eye and needs its own check here too.
#
# Anchored to a whole standalone token (\b...\b on both ends) so this never
# fires on a doubled letter buried INSIDE an ordinary word (Russian
# "аллея", English "spoonful") — only an isolated run of nothing but that
# one letter counts. Minimum length 4 (the first letter plus 3+ repeats)
# keeps it away from short tokens that are also a repeated letter purely by
# coincidence and appear constantly in ordinary text — "AA" batteries,
# "III" as a roman numeral, "www" in a URL, "ООО" as the Russian company
# suffix — none of which reach 4 in a row.
#
# NOTE: deliberately named differently from the (unrelated) _LETTER_RUN_RE
# further down in this file (used by check_mixed_script to match ANY run of
# letters, repeated or not, for homoglyph detection) — reusing that name
# here would silently shadow it at module load time, since Python just
# keeps the LAST top-level assignment to a name; the two patterns are not
# interchangeable, and this exact collision was caught live in review.
#
# Kept requiring BOTH boundaries here — an earlier attempt dropped the
# trailing \b to handle a grammatical ending glued straight onto the
# placeholder with no space (a Korean particle like "의", or a Turkic
# case/possessive suffix — Александр's ask, 2026-09-22), but that loosened
# the regex itself, so it also newly matched the FIRST 4+ letters of any
# ordinary word that happens to start with a repeated letter and keep
# going as the same word (e.g. Russian informal emphasis "оооочень", or a
# marketing "WOOOOW") — caught in review. Fixed properly in
# check_letter_placeholders below instead: this regex stays exactly as
# strict as before (so it can't newly misfire on ordinary text), and the
# glued-suffix case is handled by a separate, targeted maximal-run search
# for the EXACT letter+length already known from the source side, which
# needs no \w boundary at all (see _count_maximal_runs).
_PLACEHOLDER_LETTER_RUN_RE = re.compile(r"\b([A-Za-zА-Яа-яЁё])\1{3,}\b")


def _extract_letter_runs(text: str) -> list[str]:
    return [m.group(0) for m in _PLACEHOLDER_LETTER_RUN_RE.finditer(text)]


def _run_script(run: str) -> str:
    return "cyrillic" if _CYRILLIC_LETTER_RE.search(run) else "latin"


def _count_maximal_runs(run: str, text: str) -> int:
    """Count occurrences of `run` (a string of N repeats of one letter) in
    `text` that are a MAXIMAL same-letter run of exactly that length — not
    just any substring match. Deliberately not a plain `text.count(run)`:
    caught in review — a bare substring count can't tell a genuine 4-letter
    placeholder from 4 consecutive letters sitting INSIDE an unrelated
    longer run of the same letter (a 16-letter masked card number embeds
    every possible 4-letter substring of the same repeated letter), which
    would wrongly credit a completely different, genuinely-dropped shorter
    placeholder as still present. Boundary-free with respect to any OTHER
    character on purpose (so a run glued directly onto a following/
    preceding grammatical suffix with no space, e.g. a Korean particle or
    Turkic case ending, still counts) — only runs of the SAME letter
    immediately before/after disqualify a candidate, via the lookaround."""
    if not run:
        return 0
    letter = re.escape(run[0])
    pattern = re.compile(r"(?<!" + letter + r")" + letter + "{" + str(len(run)) + "}" + r"(?!" + letter + r")")
    return len(pattern.findall(text))


def check_letter_placeholders(source: str, translation: str) -> list[dict]:
    """Two real bugs were caught in review of the first version of this
    function (naive position-by-position pairing of every extracted run)
    and fixed here:

    (1) A source with NO letter-run placeholder at all used to short-
    circuit with an early `return []` before the translation was ever even
    looked at — so a run that appeared ONLY in the translation (e.g. a
    Cyrillic keyboard slip producing a coincidental "ХХХХХХХХ") was never
    reported at all, even though the exact same "extra placeholder" case
    WAS caught whenever the source already had at least one real one.

    (2) Pairing purely by list position broke the moment there were 2+
    placeholders and the translator reordered the clauses containing them
    (completely normal in Russian) — a survived-but-moved placeholder got
    compared against a different, unrelated one and flagged as "changed"
    even though both were actually untouched; and a placeholder genuinely
    DROPPED from the middle of several could get blamed on the wrong one
    entirely (the drop silently passed while an unrelated later one was
    wrongly reported "missing").

    Fixed by resolving every EXACT match between the two runs lists first,
    as a multiset (so reordering and duplicate placeholders are both
    handled correctly and never flagged) — only what's left over on either
    side after that gets paired up positionally, which is where a genuine
    count/alphabet change actually shows up.

    Known remaining edge case, accepted as out of scope: if a document has
    TWO OR MORE DIFFERENT placeholders that are each independently changed
    AND reordered AND their new lengths happen to cross-match each other's
    original length (e.g. a 6-letter run shrinks to 5 while a different
    5-letter run grows to 6, and the two also swap position), the leftover
    positional pairing above can match them to each other instead of to
    themselves and miss both changes. A real-world file with several
    letter-run placeholders in one cell is already an edge case on its
    own; independently changing two of them AND having the new lengths
    swap with each other is vanishingly unlikely on top of that — solving
    it properly would need a full alignment (e.g. edit-distance/
    assignment) between leftovers rather than a simple position pairing,
    which isn't worth the complexity for how rare this combination is.

    (3, added 2026-09-22) Before a leftover source run is reported as
    genuinely missing, it's checked for a boundary-free (w.r.t. any OTHER
    character) maximal same-length run of the identical letter anywhere in
    the translation, beyond however many the strict extraction already
    accounted for — that catches a placeholder that's actually still
    there but has a grammatical suffix glued directly onto it with no
    separating space (a Korean particle, a Turkic case/possessive ending),
    which the extraction regex's own \\b requirement can't see as a
    separate token, while still correctly telling apart a repeated
    placeholder text (two masked card numbers, one genuinely dropped) and
    a short placeholder from an unrelated longer run of the same letter
    elsewhere in the same cell. See _PLACEHOLDER_LETTER_RUN_RE's own
    comment for why that regex itself was deliberately left untouched
    instead of being loosened to handle this, and _count_maximal_runs'
    own docstring for why a plain substring count isn't enough here."""
    src_runs = _extract_letter_runs(source)
    tr_runs = _extract_letter_runs(translation)
    if not src_runs and not tr_runs:
        return []

    tr_pool = list(tr_runs)
    leftover_src = []
    for run in src_runs:
        if run in tr_pool:
            tr_pool.remove(run)  # consumes exactly one matching instance, so duplicates stay balanced
        else:
            leftover_src.append(run)
    leftover_tr = tr_pool

    # Glued-suffix survival check (Александр's ask, 2026-09-22): a run that
    # looks "missing" so far might just have a grammatical ending glued
    # directly onto it with no space (a Korean particle, a Turkic case
    # suffix), which is exactly what makes _PLACEHOLDER_LETTER_RUN_RE's own
    # trailing \b fail to extract it from the translation in the first
    # place. We already know the EXACT text to look for from the source
    # side, so a boundary-free (w.r.t. any OTHER character) maximal-run
    # search settles it without loosening the regex itself (and without
    # the false positives that loosening it introduced — see the regex's
    # own comment above). Uses _count_maximal_runs, not a plain substring
    # count — caught in review: a bare `text.count(run)` can't tell a
    # genuine short placeholder from that many letters sitting INSIDE an
    # unrelated longer run of the same letter (every 4-letter window of a
    # surviving 16-letter run is also, textually, "XXXX").
    #
    # Counted, not a bare "in"/">0" test either — caught in review: a naive
    # presence test credits the SAME physical occurrence to every leftover
    # entry with that text, so when the source has the same placeholder run
    # TWICE (two masked card numbers both shown as "XXXXXXXX") and only one
    # genuinely survives (glued or not) while the other is truly dropped, a
    # bare presence test would find the surviving copy and wrongly clear
    # BOTH leftovers. Only occurrences beyond what strict extraction
    # already paired up (tr_runs itself, physically the same occurrence(s)
    # already consumed above) count as "extra" glued survivors, and each
    # one is consumed at most once.
    strict_counts = Counter(tr_runs)
    glued_available = {
        run: max(0, _count_maximal_runs(run, translation) - strict_counts.get(run, 0))
        for run in set(leftover_src)
    }
    still_leftover = []
    for run in leftover_src:
        if glued_available.get(run, 0) > 0:
            glued_available[run] -= 1
        else:
            still_leftover.append(run)
    leftover_src = still_leftover

    findings = []
    for i, src_run in enumerate(leftover_src):
        if i >= len(leftover_tr):
            findings.append({
                "type": "placeholders",
                "severity": "high",
                "message": (
                    f"В исходнике есть буквенная заглушка «{src_run}» ({len(src_run)} букв) — в переводе такой "
                    "заглушки не осталось. Проверьте, не потерялась ли она."
                ),
            })
            continue
        tr_run = leftover_tr[i]
        if len(src_run) != len(tr_run):
            findings.append({
                "type": "placeholders",
                "severity": "high",
                "message": (
                    f"Буквенная заглушка «{src_run}» ({len(src_run)} букв) в переводе стала «{tr_run}» "
                    f"({len(tr_run)} букв) — количество букв в такой заглушке менять нельзя (это, скорее всего, "
                    "формат маскированных данных, например номера карты)."
                ),
            })
        elif _run_script(src_run) != _run_script(tr_run):
            findings.append({
                "type": "placeholders",
                "severity": "high",
                "message": (
                    f"Буквенная заглушка «{src_run}» в переводе набрана другим алфавитом — «{tr_run}» выглядит "
                    "так же на глаз, но это буквы кириллицы вместо латиницы (или наоборот). Замените на буквы "
                    "исходного алфавита."
                ),
            })
    if len(leftover_tr) > len(leftover_src):
        extra = leftover_tr[len(leftover_src):]
        shown = ", ".join(f"«{r}»" for r in extra)
        findings.append({
            "type": "placeholders",
            "severity": "medium",
            "message": f"В переводе появилась лишняя буквенная заглушка, которой не было в исходнике: {shown}.",
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


# Cyrillic and Latin both use the ordinary alphabet range here (no
# diacritics/extended letters needed — every real look-alike pair Александр
# asked about is a plain a-z letter on one side) — а/a, е/e, о/o, р/p, с/c,
# у/y, х/x, and their uppercase forms, plus а few more that are just as
# visually identical (В/B, Н/H, К/K, М/M, Т/T) even though they're less
# likely to be typed by accident mid-word. Used only to detect that a SINGLE
# word contains letters from BOTH alphabets — never to guess which specific
# letter is "the" mistake, since from the raw character alone there's no way
# to tell it apart from its look-alike twin.
_CYRILLIC_LETTER_RE = re.compile(r"[а-яА-ЯёЁ]")
_LATIN_LETTER_RE = re.compile(r"[a-zA-Z]")
# A "word" for this purpose only ever needs letters — digits/punctuation
# inside a token (e.g. a placeholder-ish "id123") never affect whether it
# mixes scripts, and stripping them out avoids splitting a genuine mixed-
# script run into several separately-innocent-looking pieces.
_LETTER_RUN_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ]+")


def check_mixed_script(translation: str) -> list[dict]:
    """Catches an invisible-to-the-eye typo: a word that LOOKS like it's
    written in one alphabet but actually mixes in a look-alike letter from
    the other (Cyrillic "с" typed where a Latin "c" belongs, or vice versa)
    — Александр's example was exactly this, human eyes can't tell "с" from
    "c" apart at a glance. Flags any single letter-run that contains BOTH a
    Cyrillic and a Latin letter; a legitimate brand name or code embedded in
    an otherwise-Cyrillic sentence (e.g. "используйте Google") is still a
    PURE-Latin word on its own and never trips this — only an actual mix
    WITHIN one word does. No real word in any of these languages is
    genuinely both-alphabet, so this has essentially no false-positive risk
    and needs no opt-in of its own — folded directly into check_punctuation
    below, the same way check_numbers is silently folded in whenever
    "Оформление" is ticked (see CHECK_OPTIONS/buildChecksToSend on the
    frontend)."""
    suspects = sorted({
        m.group(0) for m in _LETTER_RUN_RE.finditer(translation)
        if _CYRILLIC_LETTER_RE.search(m.group(0)) and _LATIN_LETTER_RE.search(m.group(0))
    })
    if not suspects:
        return []
    shown = ", ".join(f"«{w}»" for w in suspects[:10])
    return [{
        "type": "punctuation",
        "severity": "medium",
        "message": (
            f"В переводе есть слово(-а), где вперемешку кириллица и латиница — на глаз не видно, но буквы "
            f"разных алфавитов (например, «с»/«c», «о»/«o», «р»/«p», «х»/«x», «а»/«a», «е»/«e»): {shown}. "
            "Похоже на случайно попавшую букву другого алфавита — проверьте и исправьте."
        ),
    }]


# Emoji detection — Александр's ask (2026-09-17): the platform wasn't
# reliably catching an emoji that's present in the source but missing from
# the translation, and didn't check that an emoji in the translation is
# actually separated from surrounding text by a space, the way his promo
# copy always formats it ("...Max 🔥", "...bot 🫶"). Covers the two
# supplementary-plane blocks that hold almost every real-world emoji
# (pictographs/emoticons/transport/supplemental/extended-A, U+1F000-1FFFF —
# a deliberately wide net across that whole plane rather than every
# sub-block by name, since new emoji keep landing in gaps between the
# official sub-ranges) plus the two BMP symbol blocks that hold the rest
# (misc symbols & dingbats like ❤️✅☀️, misc symbols & arrows like ⭐⬛).
# A flag (two regional-indicator letters, e.g. 🇧🇷 = "BR" as two special
# code points with NO joiner between them) is matched as its own two-
# code-point unit FIRST, since the general branch below would otherwise
# treat each half as its own separate "emoji" — that was a real bug caught
# in review: without this, a flag's own two halves looked "unspaced" from
# each other. The general branch also groups a variation selector (️
# U+FE0F — turns a plain symbol like "❤" into its emoji-presentation form
# "❤️"), a skin-tone modifier, or a ZWJ-joined second emoji onto the same
# match, so a compound sequence counts as ONE emoji, not two or three.
_EMOJI_BASE_RANGES = "\U0001F000-\U0001FFFF" "\U00002600-\U000027BF" "\U00002B00-\U00002BFF"
_REGIONAL_INDICATOR_RANGE = "\U0001F1E6-\U0001F1FF"
EMOJI_RE = re.compile(
    "(?:[" + _REGIONAL_INDICATOR_RANGE + "]{2})"
    "|(?:[" + _EMOJI_BASE_RANGES + "])"
    "(?:[\U0001F3FB-\U0001F3FF\U0000FE0F]|\U0000200D[" + _EMOJI_BASE_RANGES + "])*"
)


def _extract_emoji(text: str) -> list[str]:
    return EMOJI_RE.findall(text)


# The real problem this check is for is an emoji glued directly to a WORD
# ("Lootbox🔥") — punctuation of any kind hugging an emoji with no space
# (either side — "Поздравляем!🎉", "(🔥 предложение)", "штуки🎉,") is
# ordinary, legitimate copy, not a spacing mistake, and neither is two+
# emoji clustered together with no space between them ("🎉🔥💰", checked
# separately below via touches_prev/touches_next). So rather than trying
# to enumerate every acceptable punctuation mark (and inevitably missing
# one), this only flags an actual LETTER or DIGIT — in any script — sitting
# directly against the emoji with nothing between them.


def check_emoji(source: str, translation: str) -> list[dict]:
    findings = []
    src_emoji = _extract_emoji(source)
    tr_emoji = _extract_emoji(translation)
    if sorted(src_emoji) != sorted(tr_emoji):
        src_counter = Counter(src_emoji)
        tr_counter = Counter(tr_emoji)
        missing = sorted((src_counter - tr_counter).elements())
        extra = sorted((tr_counter - src_counter).elements())
        parts = []
        if missing:
            parts.append(f"есть в исходнике, нет в переводе: {' '.join(missing)}")
        if extra:
            parts.append(f"есть в переводе, нет в исходнике: {' '.join(extra)}")
        findings.append({
            "type": "emoji",
            "severity": "medium",
            "message": "Эмодзи в исходнике и переводе не совпадают — " + "; ".join(parts) + ".",
        })

    # Every emoji in the translation should be set off from surrounding text
    # by a space (or sit right at the very start/end of the string, next to
    # ordinary hugging punctuation, or next to another emoji in a cluster)
    # — a separate, purely formatting concern from whether the RIGHT emoji
    # made it into the translation at all, so it's reported independently
    # and doesn't care whether the presence check above also fired.
    matches = list(EMOJI_RE.finditer(translation))
    unspaced = []
    for i, m in enumerate(matches):
        touches_prev = i > 0 and matches[i - 1].end() == m.start()
        touches_next = i + 1 < len(matches) and matches[i + 1].start() == m.end()
        before_ok = (
            m.start() == 0 or touches_prev or not translation[m.start() - 1].isalnum()
        )
        after_ok = (
            m.end() == len(translation) or touches_next or not translation[m.end()].isalnum()
        )
        if not before_ok or not after_ok:
            unspaced.append(m.group(0))
    if unspaced:
        shown = " ".join(sorted(set(unspaced)))
        findings.append({
            "type": "emoji",
            "severity": "low",
            "message": (
                f"В переводе эмодзи не отделён(ы) пробелом от текста рядом (должен быть пробел до и после, "
                f"если это не самое начало/конец строки): {shown}."
            ),
        })
    return findings


# Terminal punctuation is only reliably visible past trailing "wrapper"
# content that carries no meaning of its own — a closing HTML/XML tag
# ("</b>"), a placeholder/tag token (PLACEHOLDER_RE — "{icon}", "%s"), or a
# closing quote/bracket a sentence naturally ends inside of (", ', », ",
# ), ], }). Naive src[-1]/tr[-1] indexing (the original version of this
# check) reads "Click here.</b>" as ending in ">", not ".", and misses a
# genuinely dropped period entirely — confirmed empirically, and matches
# Александр's report (2026-09-22) that presence/absence detection wasn't
# always reliable. Stripped repeatedly since a sentence can end several
# wrapper layers deep ("...!</b>").
_TRAILING_CLOSING_RE = re.compile(r"[\"'»”’)\]}]\s*$")


_MAX_WRAPPER_STRIP_LAYERS = 50  # see the loop's own comment below


def _real_last_char(text: str) -> str:
    t = text.rstrip()
    # Bounded to a fixed number of layers, not "while changed" alone —
    # caught in review: each layer re-scans the whole (shrinking) string
    # with finditer, so a string built almost entirely of tiny trailing
    # wrapper tokens (a corrupted export gluing hundreds of empty tags
    # together) would make this quadratic in the number of layers. A real
    # sentence is never wrapped more than a handful of layers deep, so this
    # cap only ever matters for pathological input, where falling back to
    # judging whatever's left (rather than continuing to strip) is fine.
    for _ in range(_MAX_WRAPPER_STRIP_LAYERS):
        if not t:
            break
        changed = False
        # PLACEHOLDER_RE.search() alone would return the LEFTMOST match in
        # the string, not one anchored at the end — for a string with an
        # earlier tag/placeholder too ("<b>Text</b> here.<icon>"), that
        # leftmost match ("<b>") never reaches len(t), so the real trailing
        # one ("<icon>") would be silently skipped and this would fall back
        # to naive last-character indexing, the exact bug this function
        # exists to avoid. finditer() walks every non-overlapping match, so
        # the one actually touching the end of the string is found instead.
        for m in PLACEHOLDER_RE.finditer(t):
            if m.end() == len(t):
                t = t[: m.start()].rstrip()
                changed = True
                break
        if changed:
            continue
        m = _TRAILING_CLOSING_RE.search(t)
        if m:
            t = t[: m.start()].rstrip()
            changed = True
        if not changed:
            break
    return t[-1] if t else ""


# "Оформление": length long dash "—" requires spaces on both sides
# (Александр's ask, 2026-09-22) — anything glued straight onto a
# neighboring word looks like a typo, not intentional typography.
def check_em_dash_spacing(translation: str) -> list[dict]:
    bad_count = 0
    for i, ch in enumerate(translation):
        if ch != "—":
            continue
        before_ok = i == 0 or translation[i - 1].isspace()
        after_ok = i == len(translation) - 1 or translation[i + 1].isspace()
        if not before_ok or not after_ok:
            bad_count += 1
    if not bad_count:
        return []
    return [{
        "type": "punctuation",
        "severity": "low",
        "message": (
            f"В переводе длинное тире «—» стоит без пробела с одной из сторон ({bad_count} раз(а)) — "
            "вокруг «—» должны быть пробелы с обеих сторон."
        ),
    }]


# A plain hyphen "-" surrounded by SPACES on both sides is almost always a
# dash used where the translation should have an em dash "—" — a genuine
# hyphen (as in "video-game") never has spaces around it, so this is a
# reliable, purely mechanical signal for "по смыслу здесь тире, а не
# дефис" without needing AI judgment. Deliberately skipped for SMS content
# (see check_sms_charset, gated the same way in run_rule_checks below),
# where Александр's own spec requires the OPPOSITE — a plain ASCII hyphen
# and never an em dash — so this rule would be actively wrong there.
#
# Real bug caught live (Александр's report, 2026-09-22): a bulleted rules/
# T&C list flattened into one cell ("Правила: - пункт один; - пункт два;
# - пункт три.") uses "- " as a LIST MARKER, not a dash, and every bullet
# still sits between two ordinary spaces — the first version of this
# check just excluded a hyphen whenever the nearest character behind it
# was itself clause-closing punctuation (colon/semicolon/period/etc.).
# That fixed the reported case but review caught it cutting both ways
# wrong: (a) it also silently missed a genuine dash typo that happens to
# START a new sentence ("Ты гений! - воскликнул он." — a real typo, not a
# list), since "!" is exactly that kind of punctuation; and (b) it still
# misfired on a list whose OWN items aren't separated by punctuation at
# all ("Не действует на: - ставки А - ставки Б - возврат.") — only the
# first bullet sits right after the colon, so every later one was still
# flagged.
#
# The actual distinguishing signal isn't "what's the one character right
# before this hyphen" — it's "is this hyphen part of a colon-introduced
# enumeration at all": does a colon appear anywhere between the START of
# the CURRENT sentence and this hyphen (a colon that opened a list stays
# in scope until the sentence actually ends — a real sentence boundary,
# ".", "!", "?", "。", "！", "？", or a line break, not just any
# punctuation)? Or is the hyphen the very first thing in the whole cell
# at all (the list's own intro colon lives in a separate column/context,
# so there's nothing to find here)?
#
# And once EITHER of those has recognized one hyphen in this cell as a
# bullet, every LATER "- " in the same cell is treated as continuing that
# same list too ("sticky"), regardless of what punctuation (if any)
# separates one bullet from the next — otherwise a list whose own items
# aren't semicolon-separated ("Не действует на: - ставки А - ставки Б -
# возврат.") still only had its very first bullet recognized, and every
# later one was still wrongly flagged (caught in review). A genuine dash
# typo unrelated to an established list later in the very same cell is
# the one residual case this can miss — accepted, since it needs an
# unusual "list, then unrelated prose dash" combination in one cell, and
# this check is already advisory/low-severity, not a hard error.
#
# "Sentence boundary" here needs care — caught in review, three times:
# (1) a bare "\." also matched a DECIMAL point ("1.5", "4.0" — an odds
# value, exactly the kind of number this gambling-promo content is full
# of), which falsely ended the "sentence" early and dropped an earlier
# list-opening colon out of scope, flagging a perfectly normal bulleted
# list all over again. Excluded with digit lookaround on both sides.
# (2) an ellipsis "..." didn't match at all (each "." is directly
# adjacent to another one, so the original "not preceded/followed by a
# dot" exclusion silently ate the whole run) — matched as one unit here
# instead of not matching, so it still counts as a real sentence boundary
# rather than letting an earlier colon stay "in scope" indefinitely.
# (3) an abbreviation period ("т.н.", "e.g.", "U.S.") isn't adjacent to a
# digit, so the digit guard alone didn't catch it — same false-"sentence
# ended" effect as the decimal case. A genuine sentence-ending period is
# followed by whitespace/end-of-string AND does not continue straight
# into a lowercase word — an abbreviation period is either followed
# immediately by another letter with no space at all ("т.н."'s first
# dot), or by a space and then a lowercase continuation of the same
# phrase ("т.н." 's second dot, before "бонусные"). Real sentence starts
# overwhelmingly begin with a capital letter or the string simply ends,
# so this is a solid (if not airtight) practical signal — a lowercase
# letter starting a new, unrelated real sentence is the one case this
# still can't tell apart, accepted for the same reason as the sticky
# list_mode trade-off above: this check is advisory/low-severity.
# (4) the whitespace-or-end check in (3) was too literal — a period
# followed by a closing quote/bracket BEFORE the actual whitespace
# ('Он сказал: "Всё готово." Играй - выигрывай.') failed it outright, so
# the period was excluded entirely rather than recognized as ending the
# sentence, again leaving an earlier colon "in scope" for a later,
# unrelated genuine typo. Fixed the same way _real_last_char handles
# trailing wrapper content elsewhere in this file: a run of whitespace
# and/or closing quote/bracket characters right after the period is
# skipped over before judging what comes next.
# The run of whitespace/wrapper characters to skip, and the character
# class used afterwards to decide "did we land on real content, or just
# another lowercase letter continuing the same word" MUST be mutually
# exclusive sets — caught in review: with a plain "[\s...]*" followed by
# "[^a-zа-яё]", the engine could backtrack to matching ZERO wrapper
# characters and let a SPACE itself satisfy "[^a-zа-яё]" (a space isn't a
# lowercase letter either), short-circuiting past the real word that
# follows without ever looking at it — silently defeating the whole
# skip-then-check idea for exactly the "quote, then space, then a real
# word" case this was meant to fix. _TERMINATOR excludes whitespace and
# wrapper characters too, so it can only match by actually reaching past
# them to real content.
_CLOSING_WRAP_OR_SPACE = r"[\s\"'»”’)\]]"
_TERMINATOR = r"[^a-zа-яё\s\"'»”’)\]]"
_SENTENCE_BOUNDARY_RE = re.compile(
    r"\.{2,}"
    r"|(?<!\d)(?<!\.)\.(?!\.)(?!\d)(?=" + _CLOSING_WRAP_OR_SPACE + r"*(?:$|" + _TERMINATOR + r"))"
    r"|[!?？！。]"
)
_COLON_RE = re.compile(r"[:：]")

# NOTE: a bare newline is deliberately NOT a sentence boundary — caught
# live on Александр's real production content (2026-09-22): a bulleted
# list flattened into one Excel cell is very often laid out with a BLANK
# LINE between the colon-introducing header and each bullet, and between
# the bullets themselves ("правила:\n\n- пункт один;\n\n- пункт два;..."),
# purely as visual spacing — not as separate "sentences". Treating each
# newline as its own boundary reset the current-sentence scope right
# before every single bullet, dropping the list's own intro colon out of
# scope every time and reflagging the whole list all over again — this
# was the exact false positive that reopened the original production bug
# despite it already being "fixed" for the flat, no-newline version of
# the same list. A period/exclamation/question mark (or ellipsis) is
# already a strong enough signal on its own; a plain line break within
# one cell is just formatting.


def check_hyphen_for_dash(translation: str) -> list[dict]:
    hyphen_positions = [m.start() for m in re.finditer(r"(?<=\s)-(?=\s)", translation)]
    if not hyphen_positions:
        return []

    # A single pass over the whole string collects every boundary/colon
    # position up front (each O(n)); each hyphen is then resolved against
    # them with a binary search instead of re-slicing and re-scanning the
    # ever-growing prefix from scratch for every single hyphen — caught in
    # review as an O(n²) blowup on a long cell with many hyphens (exactly
    # the flattened FAQ/T&C blocks this check exists for).
    boundary_ends = [m.end() for m in _SENTENCE_BOUNDARY_RE.finditer(translation)]
    colon_positions = [m.start() for m in _COLON_RE.finditer(translation)]
    first_nonws = re.search(r"\S", translation)
    first_nonws_pos = first_nonws.start() if first_nonws else len(translation)

    count = 0
    # A hyphen that opens the cell with NO leading space at all ("- First
    # item...") never matches the (?<=\s) lookbehind above in the first
    # place — it isn't preceded by anything, let alone whitespace — so it
    # would otherwise be entirely invisible to this function and never set
    # list_mode, leaving a later, punctuation-less bullet in the same list
    # wrongly flagged. Checked directly instead.
    list_mode = translation.lstrip().startswith("- ")
    for pos in hyphen_positions:
        if list_mode:
            continue
        if pos <= first_nonws_pos:
            list_mode = True
            continue  # nothing at all before this hyphen in the whole cell — a bullet, not a typo
        # bisect_right - 1 can legitimately come out to -1 (no boundary at
        # all before this hyphen yet) — Python then happily indexes from
        # the END of the list instead of raising, silently treating the
        # LAST boundary in the whole string as if it were the start of
        # the current sentence. Guarded explicitly instead of relying on
        # negative-index wraparound.
        boundary_idx = bisect.bisect_right(boundary_ends, pos) - 1
        sentence_start = boundary_ends[boundary_idx] if boundary_idx >= 0 else 0
        colon_idx = bisect.bisect_left(colon_positions, sentence_start)
        if colon_idx < len(colon_positions) and colon_positions[colon_idx] < pos:
            list_mode = True
            continue  # this hyphen is inside a colon-introduced enumeration — a list marker, not a dash typo
        count += 1
    if not count:
        return []
    return [{
        "type": "punctuation",
        "severity": "low",
        "message": (
            f"В переводе короткий дефис «-» стоит отдельным словом, окружённым пробелами ({count} раз(а)) — "
            "похоже, здесь по смыслу должно быть длинное тире «—», а не дефис."
        ),
    }]


def check_punctuation(
    source: str, translation: str, lang_code: str = "", checks: list[str] | None = None
) -> list[dict]:
    findings = []
    lang_base = lang_code.split("-")[0].lower() if lang_code else ""
    checks = checks or []

    if lang_base not in NO_TERMINAL_PUNCT_LANGS:
        src_last = _real_last_char(source)
        tr_last = _real_last_char(translation)
        # Guarded with "src_last and"/"tr_last and" everywhere below — an
        # empty string from _real_last_char (source/translation is nothing
        # but a stripped-away placeholder, e.g. a cell that's just
        # "{icon}") would otherwise pass Python's `"" in "some string"`
        # check, which is always True, and both misfire as a false "ends in
        # this punctuation mark" and silently skip a real "doesn't end in
        # any punctuation" case — caught in review.
        if src_last and src_last in TERMINAL_PUNCT:
            if not tr_last or tr_last not in TERMINAL_PUNCT_ACCEPTABLE:
                findings.append({
                    "type": "punctuation",
                    "severity": "low",
                    "message": f"В исходнике в конце стоит «{src_last}», а перевод не заканчивается знаком препинания.",
                })
        elif src_last and src_last not in TERMINAL_PUNCT_ACCEPTABLE and tr_last and tr_last in TERMINAL_PUNCT:
            # Reverse direction — added rather than dropped — deliberately
            # only fires when the source has NO terminal punctuation at
            # all (not even a "?"/other accepted mark), to stay a clean
            # mechanical fact rather than second-guessing "?" vs "."
            # across languages, which is real judgment-call territory.
            findings.append({
                "type": "punctuation",
                "severity": "low",
                "message": (
                    f"В переводе в конце добавлен «{tr_last}», которого нет в исходнике "
                    f"(там в конце «{src_last}»)."
                ),
            })

    if "  " in translation:
        findings.append({
            "type": "punctuation",
            "severity": "low",
            "message": "В переводе есть двойной пробел.",
        })

    if "sms_charset" not in checks:
        findings += check_em_dash_spacing(translation)
        findings += check_hyphen_for_dash(translation)

    findings += check_mixed_script(translation)
    findings += check_emoji(source, translation)

    return findings


# The GSM 03.38 "default alphabet" SMS actually transmits in 7-bit-per-
# character mode — anything outside it either fails to send correctly or
# silently forces the WHOLE message into 16-bit UCS-2 (halving how many
# characters fit per SMS segment, and doubling how many segments/how much a
# long message costs to send) depending on the carrier/gateway. Deliberately
# narrower than the full real GSM 7-bit table (which also allows a handful of
# accented Western-European letters like é/ñ/ü) — Александр's own spec below
# is stricter than that on purpose, since it's written for languages that
# would otherwise rely on diacritics precisely to spell ordinary words
# (Turkish ş/ı/ğ, Romanian ș/ț, Azerbaijani ə, etc.), and those must be
# transliterated to plain Latin instead of merely tolerated — "Günaydın" ->
# "Gunaydin", not left as-is. So this list is exactly his allowed set, not
# the carrier standard's full one: Latin A-Z/a-z, digits, a specific
# punctuation set, a single ASCII hyphen (never an en/em dash), and spaces.
_SMS_SAFE_RE = re.compile(r"[^A-Za-z0-9@!?.,'\"&%=+\-/:;() ]")


def check_sms_charset(translation: str) -> list[dict]:
    """Opt-in only (see CHECK_OPTIONS's "sms_charset" entry on the
    frontend, unticked by default) — Александр only wants this run for an
    SMS deliverable, and running it against an ordinary Cyrillic/Arabic/
    CJK/etc. translation by mistake would flag nearly every character, not
    a handful of genuine problems. When it IS the right check, this is a
    plain character-set membership test, not a judgment call — no AI
    needed, and none of the ambiguity a probabilistic check would add."""
    bad_chars = sorted(set(_SMS_SAFE_RE.findall(translation)))
    if not bad_chars:
        return []
    shown = ", ".join(f"«{c}»" for c in bad_chars[:15])
    tail = "" if len(bad_chars) <= 15 else f" и ещё {len(bad_chars) - 15} символ(а/ов)"
    return [{
        "type": "sms_charset",
        "severity": "high",
        "message": (
            f"В переводе есть символы, недопустимые для SMS (GSM 7-bit): {shown}{tail}. Разрешены только "
            "латинские буквы A-Z/a-z, цифры, пробел и знаки @!?.,'\"&%=+-/:;() — без диакритики (ş, ç, ñ, ș "
            "и т.п.), без «умных»/типографских кавычек (“ ” ‘ ’), без ¿¡ и без длинного "
            "тире (—, только обычный дефис -). Замените такие символы на латинские без диакритики (например, "
            "«Günaydın» → «Gunaydin») — а если из-за этого слово меняет смысл на неприемлемый, согласуйте "
            "исключение для этой конкретной SMS с менеджером, не меняя слово молча."
        ),
    }]


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
        findings += check_letter_placeholders(source, translation)
    if "max_length" in checks:
        findings += check_max_length(translation, max_length)
    if "punctuation" in checks:
        findings += check_punctuation(source, translation, lang_code, checks)
    if "sms_charset" in checks:
        findings += check_sms_charset(translation)
    return findings
