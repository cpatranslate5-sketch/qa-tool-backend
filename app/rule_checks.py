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
    out: list[str] = []
    for m in NUMBER_RE.finditer(merged):
        tok = m.group(0)
        allow_short_date = not _near_currency_marker(merged, m.start(), m.end())
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
    if "max_length" in checks:
        findings += check_max_length(translation, max_length)
    if "punctuation" in checks:
        findings += check_punctuation(source, translation, lang_code)
    if "sms_charset" in checks:
        findings += check_sms_charset(translation)
    return findings
