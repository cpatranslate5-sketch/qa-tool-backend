"""
Numbers written as words (2026-10-06, Александр): «1» in the source may be
«one» in the translation and vice versa — not an error. The only exception
is a date written in digits: it must stay in digits (a date written in words
may be given in digits, though).

Used by rule_checks.check_numbers: a number missing on one side is accepted
when the other side has it as a word.
"""
from __future__ import annotations

import re
from functools import lru_cache

try:
    from num2words import num2words as _n2w
except Exception:  # library missing — the small built-in lists still work
    _n2w = None

MAX_WORD_NUMBER = 1000  # bigger numbers are practically never spelled out

# num2words language codes for our language keys.
_N2W_LANG = {
    "en": "en", "ru": "ru", "uk": "uk", "kk": "kz", "az": "az", "tr": "tr", "tg": "tg", "pl": "pl",
    "de": "de", "es": "es", "fr": "fr", "pt": "pt", "ar": "ar", "vi": "vi", "id": "id", "it": "it",
    "ro": "ro", "bn": "bn", "fi": "fi", "th": "th", "te": "te", "ja": "ja",
}

# Languages num2words doesn't know (or knows badly for this purpose) —
# 0..12, 100, 1000; enough for «one banner», «two days», «ten spins».
_SMALL: dict[str, dict[int, list[str]]] = {
    "uz": {0: ["nol"], 1: ["bir"], 2: ["ikki"], 3: ["uch"], 4: ["to‘rt", "to'rt", "tort"], 5: ["besh"], 6: ["olti"],
           7: ["yetti"], 8: ["sakkiz"], 9: ["to‘qqiz", "to'qqiz"], 10: ["o‘n", "o'n"], 11: ["o‘n bir", "o'n bir"],
           12: ["o‘n ikki", "o'n ikki"], 100: ["yuz"], 1000: ["ming"]},
    "ky": {0: ["нөл"], 1: ["бир"], 2: ["эки"], 3: ["үч"], 4: ["төрт"], 5: ["беш"], 6: ["алты"], 7: ["жети"],
           8: ["сегиз"], 9: ["тогуз"], 10: ["он"], 11: ["он бир"], 12: ["он эки"], 100: ["жүз"], 1000: ["миң"]},
    "hi": {0: ["शून्य"], 1: ["एक"], 2: ["दो"], 3: ["तीन"], 4: ["चार"], 5: ["पांच", "पाँच"], 6: ["छह", "छः"],
           7: ["सात"], 8: ["आठ"], 9: ["नौ"], 10: ["दस"], 11: ["ग्यारह"], 12: ["बारह"], 100: ["सौ"], 1000: ["हज़ार", "हजार"]},
    "hing": {1: ["ek"], 2: ["do"], 3: ["teen"], 4: ["char", "chaar"], 5: ["paanch", "panch"], 6: ["chhe", "che"],
             7: ["saat"], 8: ["aath"], 9: ["nau"], 10: ["das"], 100: ["sau"], 1000: ["hazaar", "hazar"]},
    "mr": {1: ["एक"], 2: ["दोन"], 3: ["तीन"], 4: ["चार"], 5: ["पाच"], 6: ["सहा"], 7: ["सात"], 8: ["आठ"],
           9: ["नऊ"], 10: ["दहा"], 100: ["शंभर"], 1000: ["हजार"]},
    "ur": {1: ["ایک"], 2: ["دو"], 3: ["تین"], 4: ["چار"], 5: ["پانچ"], 6: ["چھ"], 7: ["سات"], 8: ["آٹھ"],
           9: ["نو"], 10: ["دس"], 100: ["سو"], 1000: ["ہزار"]},
    "ms": {0: ["kosong", "sifar"], 1: ["satu", "se"], 2: ["dua"], 3: ["tiga"], 4: ["empat"], 5: ["lima"], 6: ["enam"],
           7: ["tujuh"], 8: ["lapan"], 9: ["sembilan"], 10: ["sepuluh"], 11: ["sebelas"], 12: ["dua belas"],
           100: ["seratus"], 1000: ["seribu"]},
    "tl": {1: ["isa", "isang"], 2: ["dalawa", "dalawang"], 3: ["tatlo", "tatlong"], 4: ["apat"], 5: ["lima"],
           6: ["anim"], 7: ["pito"], 8: ["walo"], 9: ["siyam"], 10: ["sampu", "sampung"], 100: ["daan"], 1000: ["libo"]},
    "sw": {1: ["moja"], 2: ["mbili", "wawili"], 3: ["tatu", "watatu"], 4: ["nne"], 5: ["tano"], 6: ["sita"],
           7: ["saba"], 8: ["nane"], 9: ["tisa"], 10: ["kumi"], 100: ["mia"], 1000: ["elfu"]},
    "el": {1: ["ένα", "ένας", "μία", "μια"], 2: ["δύο"], 3: ["τρία", "τρεις"], 4: ["τέσσερα", "τέσσερις"],
           5: ["πέντε"], 6: ["έξι"], 7: ["επτά", "εφτά"], 8: ["οκτώ", "οχτώ"], 9: ["εννέα", "εννιά"], 10: ["δέκα"],
           100: ["εκατό"], 1000: ["χίλια"]},
    "hy": {1: ["մեկ"], 2: ["երկու"], 3: ["երեք"], 4: ["չորս"], 5: ["հինգ"], 6: ["վեց"], 7: ["յոթ"], 8: ["ութ"],
           9: ["ինը"], 10: ["տասը"], 100: ["հարյուր"], 1000: ["հազար"]},
    "zh": {1: ["一", "两"], 2: ["二", "两"], 3: ["三"], 4: ["四"], 5: ["五"], 6: ["六"], 7: ["七"], 8: ["八"],
           9: ["九"], 10: ["十"], 100: ["百"], 1000: ["千"]},
    # Korean: native numerals (하나, 두 번…) — num2words gives only Sino-Korean.
    "ko": {1: ["하나", "한 ", "한번", "한 번"], 2: ["둘", "두 "], 3: ["셋", "세 "], 4: ["넷", "네 "], 5: ["다섯"],
           6: ["여섯"], 7: ["일곱"], 8: ["여덟"], 9: ["아홉"], 10: ["열"], 100: ["백"], 1000: ["천"]},
    "ja": {1: ["一つ", "ひとつ", "一"], 2: ["二つ", "ふたつ", "二"], 3: ["三つ", "みっつ", "三"], 4: ["四つ", "四"],
           5: ["五つ", "五"], 10: ["十"], 100: ["百"], 1000: ["千"]},
}

# Extra short / inflected forms num2words doesn't give («un vídeo», «eine»).
_EXTRA: dict[str, dict[int, list[str]]] = {
    "es": {1: ["un", "una"]}, "fr": {1: ["un", "une"]}, "pt": {1: ["um", "uma"], 2: ["duas"]},
    "it": {1: ["un", "una", "uno"]}, "ro": {1: ["un", "o"], 2: ["două", "doua"]},
    "de": {1: ["ein", "eine", "einen", "einem", "einer", "eines"]},
    "pl": {1: ["jeden", "jedna", "jedno", "jednego", "jednej", "jednym"], 2: ["dwie", "dwóch", "dwoch"]},
    "uk": {1: ["одна", "одне", "одного", "одній", "одним"], 2: ["дві", "двох"]},
}

# Russian: inflected forms of the small numbers (source texts are mostly Russian).
_RU_FORMS = {
    0: ["ноль", "нол", "нул"], 1: ["один", "одна", "одно", "одног", "одной", "одном", "одним", "одни"],
    2: ["два", "две", "двух", "двум", "двумя", "пара"], 3: ["три", "трёх", "трех", "трём", "трем", "тремя"],
    4: ["четыр"], 5: ["пят"], 6: ["шест"], 7: ["сем"], 8: ["восьм", "восем"], 9: ["девят"], 10: ["десят"],
    11: ["одиннадцат"], 12: ["двенадцат"], 13: ["тринадцат"], 14: ["четырнадцат"], 15: ["пятнадцат"],
    16: ["шестнадцат"], 17: ["семнадцат"], 18: ["восемнадцат"], 19: ["девятнадцат"], 20: ["двадцат"],
    30: ["тридцат"], 40: ["сорок"], 50: ["пятьдесят", "пятидесят"], 60: ["шестьдесят", "шестидесят"],
    70: ["семьдесят", "семидесят"], 80: ["восемьдесят", "восьмидесят"], 90: ["девяност"], 100: ["сто ", "сотн", "ста "],
    1000: ["тысяч"],
}

_MONTHS = {
    1: ["январ", "january", "jan"], 2: ["феврал", "february", "feb"], 3: ["март", "march", "mar"],
    4: ["апрел", "april", "apr"], 5: ["мая", "май", "may"], 6: ["июн", "june", "jun"], 7: ["июл", "july", "jul"],
    8: ["август", "august", "aug"], 9: ["сентябр", "september", "sep"], 10: ["октябр", "october", "oct"],
    11: ["ноябр", "november", "nov"], 12: ["декабр", "december", "dec"],
}

# Dates written in digits — those must stay digits.
_DATE_RE = re.compile(
    r"\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b|\b\d{4}-\d{1,2}-\d{1,2}\b|"
    r"\b\d{1,2}\s+(?:январ|феврал|март|апрел|ма[яй]|июн|июл|август|сентябр|октябр|ноябр|декабр|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\.?\s+\d{1,2}\b",
    re.IGNORECASE,
)
_DIGITS_RE = re.compile(r"\d+")


def lang_base(code: str) -> str:
    c = (code or "").strip().lower().replace("_", "-")
    if c.startswith("hi-latn") or c == "hing":
        return "hing"
    return c.split("-")[0]


def guess_source_lang(text: str) -> str:
    return "ru" if re.search(r"[а-яё]", text or "", re.IGNORECASE) else "en"


@lru_cache(maxsize=4096)
def word_forms(n: int, lang: str) -> tuple[str, ...]:
    """Lower-case spellings (or stems) of the number n in the language."""
    forms: list[str] = []
    if lang == "ru":
        forms += _RU_FORMS.get(n, [])
    forms += _SMALL.get(lang, {}).get(n, [])
    forms += _EXTRA.get(lang, {}).get(n, [])
    code = _N2W_LANG.get(lang)
    if _n2w is not None and code and lang not in ("ko",):
        for kind in ("cardinal", "ordinal"):
            try:
                w = _n2w(n, lang=code, to=kind)
            except Exception:
                continue
            if w:
                forms.append(str(w).lower())
    out = []
    for f in forms:
        f = _apos(f).lower()
        if f and f not in out:
            out.append(f)
    return tuple(out)


_NO_SPACE_SCRIPTS = ("ja", "zh", "th", "ko")
_APOS_RE = re.compile(r"[’ʼ‘`´]")


def _apos(s: str) -> str:
    return _APOS_RE.sub("'", s)


def has_number_word(text: str, n: int, lang: str) -> bool:
    """Does the text contain the number n written as a word?"""
    if n < 0 or n > MAX_WORD_NUMBER:
        return False
    low = " " + _apos(text or "").lower() + " "
    for form in word_forms(n, lang):
        if lang in _NO_SPACE_SCRIPTS or " " in form.strip() or form.endswith(" "):
            if form in low:
                return True
            continue
        # A word starting with the form: «one», «двух», «пяти», «bir»…
        stem = form if len(form) <= 4 else form[: max(4, len(form) - 2)]
        if re.search(r"(?<![\w])" + re.escape(stem) + r"\w*", low):
            # Too-short stems («он», «se», «do») must be the whole word.
            if len(form) <= 3 and not re.search(r"(?<![\w])" + re.escape(form) + r"(?![\w])", low):
                continue
            return True
    return False


def date_numbers(text: str) -> list[str]:
    """Numbers that belong to dates written in digits."""
    out: list[str] = []
    for m in _DATE_RE.finditer(text or ""):
        out += [d.lstrip("0") or "0" for d in _DIGITS_RE.findall(m.group(0))]
    return out


def month_in_words(text: str, n: int) -> bool:
    low = (text or "").lower()
    return any(re.search(r"(?<![\w])" + re.escape(w), low) for w in _MONTHS.get(n, []))
