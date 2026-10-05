"""Client styleguides (2026-10-04, Александр).

A client («Заказчик», models.Client) owns a styleguide: rules per language,
grouped in sections (tone of address, buttons, capitalization, punctuation,
ordinals, FS/FB, terms…). Every project of that client inherits them and may
override any single section for any language (models.Project.styleguide).

Each rule section is checked one of two ways:

* by the algorithm (free, exact) — the "auto" section's switches (quotes,
  dashes, ranges, ellipsis, Arabic/danda/CJK punctuation, Spanish ¡¿,
  French spaces, Thai full stop, «грн.»), ordinal endings, FS/FB spelling,
  the «Запрещено → Как надо» term table, and document-wide consistency
  (one kind of quotes / ellipsis / range dash per language file);
* by the AI — the text of every section goes into the check prompt for that
  language (see prompt_text), findings come back as type "styleguide".

Seed data: styleguide_seed.json is the client's Excel («main rules SG»),
one text per (language, row); TONE / AUTO / NOTES below add the structured
parts and the clarifications Александр gave on 2026-10-03/04.
"""
from __future__ import annotations

import copy
import json
import os
import re

# ---------------------------------------------------------------- languages --
# (key, label shown in the UI, codes in a file's header that mean this key).
# Matching is exact on these spellings — different country variants never
# fall back to each other (Александр's strict-matching rule).
LANGS: list[tuple[str, str, list[str]]] = [
    ("ar", "ar-EG", ["ar", "ar-eg"]),
    ("az", "az-AZ", ["az", "az-az"]),
    ("bn", "bn-BD", ["bn", "bd", "bn-bd"]),
    ("de", "de-DE", ["de", "de-de"]),
    ("el", "el-GR", ["el", "el-gr", "gr"]),
    ("en", "en-001", ["en", "en-001"]),
    ("es-es", "es-ES", ["es", "es-es"]),
    ("es-ar", "es-AR", ["es-ar"]),
    ("es-mx", "es-MX", ["es-mx"]),
    ("fi", "fi-FI", ["fi", "fi-fi"]),
    ("fr", "fr-CI / fr-FR", ["fr", "fr-fr", "fr-ci"]),
    ("fr-ca", "fr-CA", ["fr-ca"]),
    ("hi", "hi-IN", ["hi", "hi-in"]),
    ("hing", "hi-Latn-IN", ["hing", "hi-latn", "hi-latn-in", "hinglish"]),
    ("hy", "hy-AM", ["hy", "hy-am"]),
    ("id", "id-ID", ["id", "id-id"]),
    ("it", "it-IT", ["it", "it-it"]),
    ("ja", "ja-JP", ["ja", "jp", "ja-jp"]),
    ("kk", "kk-KZ", ["kk", "kz", "kk-kz"]),
    ("ko", "ko-KR", ["ko", "kr", "ko-kr"]),
    ("ky", "ky-KG", ["ky", "kg", "ky-kg"]),
    ("ms", "ms-MY", ["ms", "my", "ms-my"]),
    ("mr", "mr-IN", ["mr", "mr-in"]),
    ("pl", "pl-PL", ["pl", "pl-pl"]),
    ("pt-br", "pt-BR", ["pt-br", "pt", "br"]),
    ("pt-pt", "pt-PT", ["pt-pt"]),
    ("ro", "ro-MD / ro-RO", ["ro", "ro-ro", "ro-md", "md"]),
    ("ru", "ru-RU", ["ru", "ru-ru"]),
    ("sw", "sw-KE", ["sw", "sw-ke"]),
    ("tl", "tl-PH", ["tl", "fil", "tl-ph", "fil-ph", "ph"]),
    ("tg", "tg-TJ", ["tg", "tj", "tg-tj"]),
    ("te", "te-IN", ["te", "te-in"]),
    ("th", "th-TH", ["th", "th-th"]),
    ("tr", "tr-TR", ["tr", "tr-tr"]),
    ("uk", "uk-UA", ["uk", "ua", "uk-ua"]),
    ("ur", "ur-PK", ["ur", "ur-pk"]),
    ("uz", "uz-UZ", ["uz", "uz-uz"]),
    ("vi", "vi-VN", ["vi", "vn", "vi-vn"]),
    ("zh", "zh-CN", ["zh", "cn", "zh-cn", "zh-hans"]),
]
LANG_LABEL = {k: label for k, label, _ in LANGS}
LANG_KEYS = [k for k, _, _ in LANGS]
_ALIAS_TO_KEY = {a: k for k, _, aliases in LANGS for a in aliases}


def lang_key_for(code: str) -> str | None:
    """The styleguide language key for a file's language code, or None."""
    return _ALIAS_TO_KEY.get((code or "").strip().lower())


def feedback_lang_key(code: str) -> str:
    """Language key translator feedback is grouped by: the styleguide key
    when there is one, otherwise the code itself (lower-case)."""
    return lang_key_for(code) or (code or "").strip().lower()


# ----------------------------------------------------------------- sections --
# (key, Russian title, who checks it). "ai" sections are a single {"text": ...}.
SECTIONS: list[tuple[str, str, str]] = [
    ("tone", "Обращение", "ai"),
    ("cta_buttons", "Кнопки CTA", "ai"),
    ("service_buttons", "Служебные кнопки", "ai"),
    ("capitalization", "Заглавные буквы", "ai"),
    ("period_comma", "Точка и запятая", "ai"),
    ("marks", "Знаки ; : ! ?", "ai"),
    ("quotes", "Кавычки", "ai"),
    ("hyphen", "Дефис", "ai"),
    ("tilde", "Тильда", "ai"),
    ("en_dash", "Короткое тире (–)", "ai"),
    ("em_dash", "Длинное тире (—)", "ai"),
    ("ordinals", "Порядковые числительные", "ai"),
    ("versus", "Матчи (vs)", "ai"),
    ("number_sign", "Знак номера", "ai"),
    ("specific", "Особые требования", "ai"),
    ("fs_fb", "FS/FB", "ai"),
    ("other", "Прочие правила для ИИ", "ai"),
    ("terms", "Термины: запрещено → как надо", "algo"),
    ("auto", "Автоматические проверки", "algo"),
]
SECTION_KEYS = [k for k, _, _ in SECTIONS]
SECTION_TITLE = {k: t for k, t, _ in SECTIONS}

TONE_LEVELS = {
    "formal": "формальное («вы»)",
    "informal": "неформальное («ты»)",
    "neutral": "нейтральное, вежливое",
    "formal_or_neutral": "формальное или нейтральное",
    "any": "любое (неформальное допустимо)",
    "": "не задано",
}

QUOTE_KINDS = {
    "straight": '"…"',
    "curly": "“…”",
    "de_low": "„…“",
    "pl_low": "„…”",
    "guillemets": "«…»",
    "corner": "「…」",
}
RANGE_SIGNS = {
    "any": "не проверять",
    "en": "короткое тире (5–10)",
    "en_or_hyphen": "короткое тире или дефис",
    "hyphen": "дефис (5-10)",
    "tilde": "тильда (5~10 / 5〜10)",
}
RANGE_SPACES = {"any": "не проверять", "none": "без пробелов", "both": "с пробелами"}
EM_DASH_MODES = {
    "any": "не проверять",
    "forbidden": "запрещено",
    "spaced": "с пробелами с обеих сторон",
    "unspaced": "без пробелов",
}

AUTO_DEFAULT = {
    "quotes": [],               # allowed kinds; [] = don't check the kind
    "em_dash": "any",
    "en_dash_forbidden": False,
    "hyphen_forbidden": False,
    "ranges": "any",
    "range_spaces": "any",
    "ellipsis_char": False,     # only «…», never «...»
    "arabic_punct": False,      # ، and ؟
    "danda": False,             # । at sentence end (bn, hi)
    "cjk_punct": "",            # "ja" | "zh" — full-width punctuation
    "zh_latin_space": False,    # space between Chinese and Latin/digits
    "es_opening_marks": False,  # ¡…! ¿…?
    "fr_spaces": False,         # space before : ; ! ? and inside « »
    "no_final_period": False,   # Thai
    "no_period_after_currency": False,  # Ukrainian «грн.»
    "ordinals": True,           # malformed ordinal endings
    "fs_fb_latin": True,        # short form only as Latin «FS»/«FB»
    "consistency": True,        # one kind of quotes/ellipsis/range dash per file
}


# ------------------------------------------------------------- seed (1win) --
def _tone(level: str, form: str = "", avoid: str = "") -> dict:
    return {"level": level, "form": form, "avoid": avoid}


TONE = {
    "ar": _tone("neutral", "أنت (мужской род, единственное число)"),
    "az": _tone("formal", "siz"),
    "bn": _tone("formal", "আপনি"),
    "de": _tone("formal", "Sie"),
    "el": _tone("formal", "εσείς"),
    "es-es": _tone("informal", "tú"),
    "es-ar": _tone("informal", "tú", "vos (voseo)"),
    "es-mx": _tone("informal", "tú"),
    "fr": _tone("formal", "vous"),
    "hi": _tone("formal", "आप"),
    "hing": _tone("formal", "aap"),
    "id": _tone("formal_or_neutral", "Anda"),
    "it": _tone("informal", "tu"),
    "ja": _tone("formal", "です／ます"),
    "kk": _tone("formal", "сіз"),
    "ko": _tone("formal", "회원님 (или без местоимения)"),
    "ky": _tone("formal", "сиз (со строчной)"),
    "ms": _tone("formal", "", "kita (только в SMM)"),
    "mr": _tone("formal", "तुम्ही"),
    "pl": _tone("neutral", "ty"),
    "pt-br": _tone("informal", "você"),
    "pt-pt": _tone("informal", "tu", "você"),
    "ro": _tone("formal", "dvs."),
    "sw": _tone("neutral"),
    "tl": _tone("informal", "ikaw / ka / mo; kayo — для общей аудитории"),
    "tg": _tone("formal", "Шумо"),
    "te": _tone("formal", "మీరు"),
    "th": _tone("formal", "คุณ"),
    "tr": _tone("formal", "siz (современные формы -ın/-in/-un/-ün)", "устаревшие -ınız/-iniz"),
    "uk": _tone("formal", "ви / Ви (Ви/Ваш с заглавной в личном обращении)"),
    "ur": _tone("formal", "آپ"),
    "uz": _tone("formal", "Siz"),
    "vi": _tone("neutral", "bạn"),
    "zh": _tone("formal", "您"),
}

_Q = QUOTE_KINDS
AUTO = {
    "ar": {"quotes": ["straight", "curly"], "em_dash": "spaced", "ranges": "en", "arabic_punct": True},
    "az": {"quotes": ["curly"], "em_dash": "spaced", "ranges": "en"},
    "bn": {"quotes": ["straight", "curly"], "ranges": "en", "danda": True},
    "de": {"quotes": ["de_low"], "em_dash": "forbidden", "ranges": "en", "range_spaces": "none", "ellipsis_char": True},
    "el": {"quotes": ["curly", "guillemets"], "em_dash": "unspaced", "ranges": "en", "range_spaces": "none"},
    "es-es": {"quotes": ["guillemets", "curly"], "em_dash": "forbidden", "ranges": "en", "range_spaces": "none", "es_opening_marks": True},
    "es-ar": {"quotes": ["curly"], "em_dash": "forbidden", "ranges": "en", "range_spaces": "none", "es_opening_marks": True},
    "es-mx": {"quotes": ["curly"], "em_dash": "forbidden", "ranges": "en", "range_spaces": "none", "es_opening_marks": True},
    "fr": {"quotes": ["guillemets", "straight"], "ranges": "en", "fr_spaces": True},
    "hi": {"quotes": ["straight", "curly"], "ranges": "en_or_hyphen", "range_spaces": "none", "danda": True},
    "hing": {"quotes": ["straight", "curly"], "ranges": "en_or_hyphen", "range_spaces": "none"},
    "id": {"quotes": ["straight", "curly"], "em_dash": "unspaced"},
    "it": {"quotes": ["straight", "curly"], "em_dash": "spaced", "ranges": "en", "range_spaces": "none"},
    "ja": {"quotes": ["corner"], "em_dash": "unspaced", "ranges": "tilde", "cjk_punct": "ja"},
    "kk": {"quotes": ["guillemets"], "em_dash": "spaced", "en_dash_forbidden": True},
    "ko": {"ranges": "tilde", "fs_fb_latin": False},
    "ky": {"quotes": ["straight", "curly", "guillemets"], "ranges": "en"},
    "ms": {"quotes": ["curly"], "ranges": "en", "range_spaces": "none"},
    "mr": {"quotes": ["straight", "curly"], "ranges": "en_or_hyphen", "range_spaces": "none"},
    "pl": {"quotes": ["pl_low"], "ranges": "en"},
    "pt-br": {"quotes": ["curly"], "em_dash": "forbidden", "ranges": "en", "range_spaces": "none"},
    "pt-pt": {"quotes": ["guillemets"], "em_dash": "forbidden", "ranges": "en", "range_spaces": "none"},
    "ro": {"quotes": ["pl_low"], "ranges": "en", "range_spaces": "both"},
    "sw": {"quotes": ["curly"], "em_dash": "spaced", "ranges": "en", "range_spaces": "none"},
    "tl": {"quotes": ["curly"], "em_dash": "spaced", "ranges": "en", "range_spaces": "none"},
    "tg": {"quotes": ["curly", "guillemets"], "em_dash": "spaced", "ranges": "en", "range_spaces": "none"},
    "te": {"quotes": ["curly"], "ranges": "en"},
    "th": {"quotes": ["curly"], "em_dash": "spaced", "ranges": "en", "range_spaces": "both", "no_final_period": True},
    "tr": {"quotes": ["curly"], "ranges": "hyphen", "range_spaces": "none"},
    "uk": {"quotes": ["guillemets"], "em_dash": "spaced", "ranges": "en", "range_spaces": "none", "no_period_after_currency": True},
    "ur": {"quotes": ["straight", "curly"], "em_dash": "spaced", "ranges": "en", "range_spaces": "none", "hyphen_forbidden": True},
    "uz": {"quotes": ["curly"], "em_dash": "spaced", "ranges": "en", "range_spaces": "none"},
    "vi": {"quotes": ["curly"], "em_dash": "unspaced", "ranges": "en_or_hyphen", "range_spaces": "none"},
    "zh": {"quotes": ["curly"], "cjk_punct": "zh", "zh_latin_space": True},
}

# Clarifications agreed with Александр, appended to the Excel texts.
NOTES = {
    ("fr", "marks"): "Уточнение: перед : ; ! ? допустим обычный или неразрывный пробел; после знака тоже нужен пробел, кроме конца предложения/сегмента.",
    ("fr", "ordinals"): "Уточнение: только «1ère»; «1re» — ошибка.",
    ("ar", "service_buttons"): "Уточнение: повелительное наклонение на служебной кнопке — ошибка.",
    ("vi", "en_dash"): "Уточнение: диапазоны пишутся без пробелов (1-3 / 1–3), как в примере «Cấp độ phụ 1-3».",
    ("ms", "tone"): "Уточнение: kami — так компания говорит о себе (не kita; kita только в SMM). Конкретная форма обращения к игроку не задана.",
}
FS_FB_NOTE = (
    "Уточнение: длинную форму не проверять. Если используется сокращение — только латиницей «FS»/«FB»; "
    "любые другие сокращения (например, «ФС/ФБ», транслитерация) — ошибка."
)
KO_FS_FB = "Только 프리스핀 / 프리벳. Латинские FS/FB в корейском запрещены всегда."


def _seed_texts() -> dict:
    path = os.path.join(os.path.dirname(__file__), "styleguide_seed.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def build_client_seed() -> dict:
    """The full 1win client styleguide: {lang: {section: value}}."""
    texts = _seed_texts()
    out: dict = {}
    for lang in LANG_KEYS:
        t = texts.get(lang)
        if t is None and lang not in TONE:
            continue  # en/ru/hy: no main rules (tone only, per project)
        t = dict(t or {})
        rules: dict = {}
        for sec in SECTION_KEYS:
            if sec in ("terms", "auto", "tone"):
                continue
            text = t.get(sec, "")
            note = NOTES.get((lang, sec))
            if sec == "fs_fb":
                text = KO_FS_FB if lang == "ko" else (text + "\n" + FS_FB_NOTE).strip()
            if note:
                text = (text + "\n" + note).strip()
            if text:
                rules[sec] = {"text": text}
        tone = dict(TONE.get(lang) or _tone(""))
        tone["note"] = (t.get("tone", "") + ("\n" + NOTES[(lang, "tone")] if (lang, "tone") in NOTES else "")).strip()
        rules["tone"] = tone
        rules["terms"] = {"items": []}
        if lang == "ko":
            rules["terms"] = {"items": [
                {"bad": "FS", "good": "프리스핀", "note": "латинское сокращение запрещено"},
                {"bad": "FB", "good": "프리벳", "note": "латинское сокращение запрещено"},
            ]}
        auto = dict(AUTO_DEFAULT)
        auto.update(AUTO.get(lang, {}))
        rules["auto"] = auto
        out[lang] = rules
    return out


def _tov(level: str, note: str = "") -> dict:
    return {"level": level, "form": "", "avoid": "", "note": note}


# Project overrides from the client's ToV sheet (only where they differ from
# main; 1win and WL regular follow main — Александр, 2026-10-04).
PROJECTS_SEED_V1: list[tuple[str, dict]] = [
    ("1win", {}),
    ("1win SMM", {"ru": {"tone": _tov("informal", "ToV: informal")}}),
    ("WL regular", {}),
    ("Blogger", {
        "ar": {"tone": _tov("any", "ToV: Can be very informal")},
        "az": {"tone": _tov("informal", "ToV: informal")},
        "en": {"tone": _tov("informal", "ToV: informal")},
        "ky": {"tone": _tov("informal", "ToV: informal")},
        "ro": {"tone": _tov("informal", "ToV: informal")},
        "ru": {"tone": _tov("informal", "ToV: informal")},
        "tg": {"tone": _tov("informal", "ToV: informal")},
        "uz": {"tone": _tov("informal", "ToV: informal")},
    }),
    ("Jetton", {
        "en": {"tone": _tov("informal", "ToV: informal")},
        "id": {"tone": _tov("informal", "ToV: informal")},
        "pl": {"tone": _tov("informal", "ToV: informal")},
        "ru": {"tone": _tov("informal", "ToV: informal")},
        "tg": {"tone": _tov("informal", "ToV: informal")},
        "tr": {"tone": _tov("informal", "ToV: informal")},
    }),
    ("TonPlay", {
        "en": {"tone": _tov("informal", "ToV: informal")},
        "id": {"tone": _tov("informal", "ToV: informal")},
        "pl": {"tone": _tov("informal", "ToV: informal")},
        "ru": {"tone": _tov("informal", "ToV: informal")},
        "tg": {"tone": _tov("informal", "ToV: informal")},
        "uk": {"tone": _tov("informal", "ToV: informal")},
    }),
    ("WinGram", {
        "en": {"tone": _tov("informal", "ToV: informal")},
        "pl": {"tone": _tov("informal", "ToV: informal")},
        "uk": {"tone": _tov("informal", "ToV: informal")},
    }),
]


# Version 2 (2026-10-04): read with merged cells expanded — in the ToV
# sheet one «formal»/«informal» cell often spans many language rows, which
# version 1 missed. 1win / SMM / WL follow main where they differ (main has
# no rules for en, hy, ru — those take the ToV value). Blogger's general
# comment «Can be very informal» spans every language.
_BLOGGER = "ToV: informal. Can be very informal"
_NO_MAIN_1WIN = {
    "en": {"tone": _tov("formal", "ToV: formal")},
    "hy": {"tone": _tov("formal", "ToV: formal")},
}
PROJECTS_SEED: list[tuple[str, dict]] = [
    ("1win", {**_NO_MAIN_1WIN, "ru": {"tone": _tov("formal", "ToV: formal")}}),
    ("1win SMM", {**_NO_MAIN_1WIN, "ru": {"tone": _tov("informal", "ToV: informal")}}),
    ("WL regular", {**_NO_MAIN_1WIN, "ru": {"tone": _tov("formal", "ToV: formal")}}),
    ("Blogger", {k: {"tone": _tov("informal", _BLOGGER)} for k in ("az", "en", "ky", "ro", "ru", "tg", "uz")}),
    ("Jetton", {
        **{k: {"tone": _tov("informal", "ToV: informal")} for k in (
            "en", "es-mx", "fr", "hi", "hing", "id", "pl", "ru", "tg", "tr", "uk")},
    }),
    ("TonPlay", {k: {"tone": _tov("informal", "ToV: informal")} for k in ("en", "id", "pl", "ru", "tg", "uk")}),
    ("WinGram", {k: {"tone": _tov("informal", "ToV: informal")} for k in ("en", "pl", "uk")}),
]


# Each project's languages = the languages that have a tone of address in
# the client's ToV sheet for that project (Александр, 2026-10-04). Stored as
# the project's «Языки проекта» catalog, in the codes the files use.
_ALL_TOV = [
    "ar-eg", "az-az", "bn-bd", "de-de", "el-gr", "en-001", "es-es", "es-ar", "es-mx", "fr-ci", "fr-fr",
    "hi-in", "hi-latn-in", "hy-am", "id-id", "it-it", "ja-jp", "kk-kz", "ko-kr", "ky-kg", "ms-my", "mr-in",
    "pl-pl", "pt-br", "pt-pt", "ro-md", "ro-ro", "ru-ru", "sw-ke", "tl-ph", "tg-tj", "te-in", "th-th",
    "tr-tr", "uk-ua", "ur-pk", "uz-uz", "vi-vn", "zh-cn",
]
PROJECT_LANGS: dict[str, list[str]] = {
    "1win": _ALL_TOV,
    "1win SMM": _ALL_TOV,
    "WL regular": _ALL_TOV,
    "Blogger": ["az-az", "en-001", "ky-kg", "ro-md", "ro-ro", "ru-ru", "tg-tj", "uz-uz"],
    "Jetton": ["az-az", "en-001", "es-ar", "es-mx", "fr-ci", "fr-fr", "hi-in", "hi-latn-in", "id-id", "kk-kz",
               "pl-pl", "pt-br", "ru-ru", "tg-tj", "tr-tr", "uk-ua", "uz-uz"],
    "TonPlay": ["az-az", "en-001", "id-id", "kk-kz", "pl-pl", "ru-ru", "tg-tj", "uk-ua", "uz-uz"],
    "WinGram": ["en-001", "pl-pl", "uk-ua", "uz-uz"],
}


def keys_for_codes(codes) -> list[str]:
    """Styleguide language keys for a list of file/catalog codes."""
    out: list[str] = []
    for c in codes or []:
        k = lang_key_for(c)
        if k and k not in out:
            out.append(k)
    return out


# ---------------------------------------------------------------- merging ---
def effective_rules(client_sg: dict | None, project_sg: dict | None) -> dict:
    """{lang: {section: value}} — the project's own sections win."""
    out = copy.deepcopy(client_sg or {})
    for lang, secs in (project_sg or {}).items():
        target = out.setdefault(lang, {})
        for sec, val in (secs or {}).items():
            target[sec] = copy.deepcopy(val)
    return out


def rules_for_code(effective: dict | None, code: str) -> dict | None:
    if not effective:
        return None
    key = lang_key_for(code)
    if key is None:
        return None
    rules = effective.get(key)
    return rules or None


def auto_of(rules: dict | None) -> dict:
    a = dict(AUTO_DEFAULT)
    if rules and isinstance(rules.get("auto"), dict):
        a.update(rules["auto"])
    return a


# ------------------------------------------------------------- AI prompt ----
def tone_requirement_text(rules: dict | None) -> str:
    tone = (rules or {}).get("tone") or {}
    level = tone.get("level") or ""
    if not level and not (tone.get("form") or "").strip():
        return ""
    parts = [TONE_LEVELS.get(level, level)]
    if (tone.get("form") or "").strip():
        parts.append(f"форма: {tone['form'].strip()}")
    if (tone.get("avoid") or "").strip():
        parts.append(f"нежелательно: {tone['avoid'].strip()}")
    return "; ".join(parts)


def prompt_text(rules: dict | None) -> str:
    """The language's styleguide as a prompt block (empty when none)."""
    if not rules:
        return ""
    lines = []
    tone = rules.get("tone") or {}
    req = tone_requirement_text(rules)
    if req or (tone.get("note") or "").strip():
        line = f"- Обращение к пользователю: {req or 'см. пояснение'}."
        if (tone.get("note") or "").strip():
            line += f" Пояснение заказчика: {tone['note'].strip()}"
        lines.append(line)
    for sec in SECTION_KEYS:
        if sec in ("tone", "terms", "auto"):
            continue
        text = ((rules.get(sec) or {}).get("text") or "").strip()
        if text:
            lines.append(f"- {SECTION_TITLE[sec]}: {text}")
    if not lines:
        return ""
    return (
        "СТАЙЛГАЙД ЗАКАЗЧИКА ДЛЯ ЭТОГО ЯЗЫКА — обязательные требования. Каждое нарушение — отдельная находка "
        'с "type": "styleguide" (сообщай даже мелкие нарушения; в message — что написано и как должно быть '
        "по стайлгайду). Правило про обращение проверяй по местоимениям И по формам глаголов. Кнопку (CTA или "
        "служебную) определяй по контексту/ключу строки (button, btn, cta…), а если пометки нет — по самому "
        "тексту, и тогда снижай уверенность. Порядковые: окончание после числа не обязательно (в исходнике "
        "«1st», в переводе «1» — нормально), но если окончание написано, оно должно строго соответствовать "
        "правилу. Чисто механические вещи — вид кавычек, вид тире и пробелы вокруг него, «...» вместо «…», "
        "¡¿ в испанском, пробелы перед знаками во французском, данду, полноширинные знаки, запрещённые "
        "термины из списка — уже проверяет отдельный алгоритм: их НЕ повторяй.\n"
        + "\n".join(lines)
    )


# ------------------------------------------------------- algorithmic part ---
_MASK = ""
_TAG_RE = re.compile(r"\{\{?[^}]+\}?\}|%\d*\$?[sd]|<[^>]+>|\[[^\]]+\]|\\u[0-9a-fA-F]{4}|https?://\S+|www\.\S+")


def _mask(text: str) -> str:
    return _TAG_RE.sub(_MASK, text or "")


def _snip(text: str, start: int, end: int, pad: int = 18) -> str:
    a = max(0, start - pad)
    b = min(len(text), end + pad)
    s = text[a:b].replace(_MASK, "{…}").replace("\n", " ")
    return ("…" if a > 0 else "") + s + ("…" if b < len(text) else "")


def _f(message: str, severity: str = "low") -> dict:
    return {"type": "style_rule", "severity": severity, "message": message, "confidence": 100}


def quote_kinds(text: str) -> set[str]:
    """Double-quote kinds used in a text (nested single quotes ignored)."""
    kinds: set[str] = set()
    t = _mask(text)
    if '"' in t:
        kinds.add("straight")
    if "«" in t or "»" in t:
        kinds.add("guillemets")
    if "「" in t or "」" in t:
        kinds.add("corner")
    i = 0
    while i < len(t):
        ch = t[i]
        if ch == "„":
            close = next((j for j in range(i + 1, len(t)) if t[j] in "“”"), None)
            if close is None:
                kinds.add("de_low")
                i += 1
                continue
            kinds.add("de_low" if t[close] == "“" else "pl_low")
            i = close + 1
            continue
        if ch in "“”":
            kinds.add("curly")
        i += 1
    return kinds


_NESTED_OK = {"pt-pt": {"curly"}, "uk": {"straight"}, "es-es": {"curly"}}

_RANGE_RE = re.compile(
    r"(?<![\d.,:/\-–])(\d{1,4}(?:[.,:]\d{1,2})?)(\s*)([-–—~〜～])(\s*)(\d{1,4}(?:[.,:]\d{1,2})?)(?![\d/\-–]|[.,:]\d)"
)
_RANGE_SIGN_OK = {
    "en": {"–"},
    "en_or_hyphen": {"–", "-"},
    "hyphen": {"-"},
    "tilde": {"~", "〜", "～"},
}

_HAN = "一-鿿㐀-䶿"
_JA = "぀-ヿ一-鿿"
_ARABIC = "؀-ۿ"
_INDIC_BN = "ঀ-৿"
_INDIC_DEV = "ऀ-ॿ"
_THAI = "฀-๿"

# Malformed ordinal endings per language: (regex, what's right).
_ORD_EN = (re.compile(r"(?<![A-Za-z])\d+(?:st|nd|rd|th)(?![A-Za-z])", re.I), "английские окончания не используются")
ORDINAL_PATTERNS: dict[str, list[tuple[re.Pattern, str]]] = {
    "fr": [(re.compile(r"(?<!\w)(?:\d+(?:ème|eme|è|ere|re|nd|nde|ier|ière)|1e)(?!\w)"), "1er / 1ère / 2e")],
    "es-es": [(re.compile(r"(?<![\w.])\d+(?:[ºª°]|\.?(?:er|ro|do|to|vo|no|mo|ra|da|ta)\b|\.[oa]\b)"), "1.º / 1.ª")],
    "pt-br": [(re.compile(r"(?<![\w])\d+(?:\.[ºª]|°|[oa]\b)"), "1º / 1ª (без точки)")],
    "pt-pt": [(re.compile(r"(?<![\w.])\d+(?:[ºª°]|[oa]\b)"), "1.º / 1.ª")],
    "de": [(re.compile(r"(?<![\w])\d+(?:te|ter|tes|ten|ste|sten)\b"), "1. (число с точкой)")],
    "pl": [(re.compile(r"(?<![\w])\d+-?(?:szy|gi|ci|ty|my|wszy|ga|go)\b"), "1. (число с точкой)")],
    "tr": [(re.compile(r"(?<![\w])\d+['’\-]?(?:inci|ıncı|uncu|üncü|nci|ncı|ncu|ncü)\b"), "1. (число с точкой)")],
    "az": [(re.compile(r"(?<![\w\-])\d+\s*(?:ci|cı|cu|cü|nci|ncı|ncu|ncü|inci|ıncı|uncu|üncü)\b"), "1-ci (через дефис)")],
    "kk": [(re.compile(r"(?<![\w\-])\d+\s*(?:ші|шы|інші|ыншы)\b"), "через дефис: 3-ші / 3-орын")],
    "ky": [(re.compile(r"(?<![\w\-])\d+\s*(?:чи|чу|чү|инчи|ынчы|унчу|үнчү|нчи|нчы|нчу|нчү)\b"), "через дефис: 1-орун")],
    "uz": [
        (re.compile(r"(?<![\w\-])\d+\s*(?:chi|inchi|nchi)\b"), "через дефис: 1-o‘rin"),
        (re.compile(r"\b[IVXLC]+-[A-Za-z]"), "после римских цифр дефис не ставится"),
    ],
    "tg": [(re.compile(r"(?<![\w\-])\d+\s*(?:ум|юм|ом)\b"), "через дефис: 1-ум")],
    "uk": [
        (re.compile(r"(?<![\w\-])\d+(?:й|га|го|му|ий|ій|ша|ше)\b"), "через дефис: 1-й, 1-го"),
        (re.compile(r"(?<![\w])\d+-(?:ий|ій|ого|ому|ая|ое)\b"), "короткое окончание: 1-й, 1-го"),
    ],
    "bn": [(re.compile(r"\d+-(?:ম|য়|র্থ|ষ্ঠ|তম)"), "слитно: 1ম")],
    "hi": [(re.compile(r"\d+-(?:वाँ|वां|वीं|वें|ला|ली|ले|रा|री|रे|था|थी|थे)"), "слитно: 21वाँ")],
    "mr": [(re.compile(r"\d+-(?:ले|ला|ली|वा|वी|वे|रा|री|रे|था|थी|थे)"), "слитно: 1ले")],
    "te": [(re.compile(r"\d+-(?:వ|వది)"), "слитно: 1వ")],
    "id": [(re.compile(r"\bke(?:\s+|)\d+", re.I), "ke-1 (через дефис)")],
    "ms": [(re.compile(r"\bke(?:\s+|)\d+", re.I), "ke-2 (через дефис)")],
    "el": [(re.compile(r"\d+-(?:ος|η|ο|ης|ου|οι|ες)\b"), "слитно: 1ος")],
    "ro": [(re.compile(r"(?<![\w\-])\d+\s*lea\b"), "al 2-lea (через дефис)")],
    "tl": [(re.compile(r"\bika\s*\d+", re.I), "Ika-1 (через дефис)")],
}
ORDINAL_PATTERNS["es-ar"] = ORDINAL_PATTERNS["es-es"]
ORDINAL_PATTERNS["es-mx"] = ORDINAL_PATTERNS["es-es"]

_FS_FB_BAD = re.compile(
    r"(?<![A-Za-zА-Яа-яЁё0-9])(?:ФС|ФБ|Фс|Фб|фс|фб|ΦΣ|ΦΜ|fs|fb|Fs|Fb|fS|fB)(?![A-Za-zА-Яа-яЁё0-9])"
)
_ASCII_WORD = re.compile(r"^[A-Za-z0-9]")


def _term_regex(term: str, lang: str) -> re.Pattern:
    esc = re.escape(term)
    if term.isascii():
        return re.compile(rf"(?<![A-Za-z0-9]){esc}(?![A-Za-z0-9])")
    if lang in ("ja", "zh", "th", "ko"):
        return re.compile(esc)
    return re.compile(rf"(?<!\w){esc}(?!\w)")


def check_row(source: str, translation: str, rules: dict | None, lang: str) -> list[dict]:
    """Algorithmic styleguide checks of one translation."""
    if not rules or not (translation or "").strip():
        return []
    a = auto_of(rules)
    t = _mask(translation)
    out: list[dict] = []
    seen: set[str] = set()

    def add(msg: str, sev: str = "low"):
        if msg not in seen:
            seen.add(msg)
            out.append(_f(msg, sev))

    # Quotes
    allowed = set(a.get("quotes") or [])
    if allowed:
        used = quote_kinds(translation)
        nested_ok = _NESTED_OK.get(lang, set()) if used & allowed else set()
        for k in sorted(used - allowed - nested_ok):
            need = " или ".join(QUOTE_KINDS[x] for x in a["quotes"])
            add(f"Кавычки {QUOTE_KINDS[k]} не по стайлгайду — нужны {need}.")

    # Em dash
    mode = a.get("em_dash") or "any"
    for m in re.finditer("—", t):
        before = t[m.start() - 1] if m.start() > 0 else ""
        after = t[m.end()] if m.end() < len(t) else ""
        if mode == "forbidden":
            add(f"Длинное тире «—» запрещено стайлгайдом: «{_snip(t, m.start(), m.end())}».")
            break
        if mode == "spaced" and before and after and (not before.isspace() or not after.isspace()):
            if not (before.isdigit() and after.isdigit()):
                add(f"Длинное тире без пробелов: «{_snip(t, m.start(), m.end())}» — по стайлгайду пробелы с обеих сторон.")
        if mode == "unspaced" and ((before and before.isspace()) or (after and after.isspace())):
            add(f"Длинное тире с пробелами: «{_snip(t, m.start(), m.end())}» — по стайлгайду без пробелов.")

    if a.get("en_dash_forbidden") and "–" in t:
        i = t.index("–")
        add(f"Короткое тире «–» запрещено стайлгайдом: «{_snip(t, i, i + 1)}».")

    if a.get("hyphen_forbidden"):
        for m in re.finditer(r"-", t):
            b = t[m.start() - 1] if m.start() > 0 else ""
            c = t[m.end()] if m.end() < len(t) else ""
            if b.isdigit() and c.isdigit():
                continue
            if c.isdigit() and (not b or b.isspace()):
                continue  # minus sign
            add(f"Дефис «-» запрещён стайлгайдом: «{_snip(t, m.start(), m.end())}».")
            break

    # Number ranges
    rng = a.get("ranges") or "any"
    rsp = a.get("range_spaces") or "any"
    for m in _RANGE_RE.finditer(t):
        sign, sp1, sp2 = m.group(3), m.group(2), m.group(4)
        frag = m.group(0)
        ok = _RANGE_SIGN_OK.get(rng)
        if sign == "—":
            if rng != "any":
                add(f"Диапазон «{frag}» через длинное тире — по стайлгайду: {RANGE_SIGNS[rng]}.")
            continue
        if ok is not None and sign not in ok:
            add(f"Диапазон «{frag}» — по стайлгайду: {RANGE_SIGNS[rng]}.")
            continue
        if rsp == "none" and (sp1 or sp2):
            add(f"Диапазон «{frag}» с пробелами — по стайлгайду без пробелов.")
        elif rsp == "both" and (not sp1 or not sp2):
            add(f"Диапазон «{frag}» без пробелов — по стайлгайду с пробелами с обеих сторон.")

    if a.get("ellipsis_char") and "..." in t:
        i = t.index("...")
        add(f"Три точки «...» — по стайлгайду нужен один символ многоточия «…»: «{_snip(t, i, i + 3)}».")

    if a.get("arabic_punct"):
        if re.search(rf"[{_ARABIC}]\s*,|,\s*[{_ARABIC}]", t):
            m = re.search(rf"[{_ARABIC}]\s*,|,\s*[{_ARABIC}]", t)
            add(f"Латинская запятая «,» в арабском тексте — нужна «،»: «{_snip(t, m.start(), m.end())}».")
        m = re.search(rf"[{_ARABIC}]\s*\?", t)
        if m:
            add(f"Латинский вопросительный знак «?» — нужен «؟»: «{_snip(t, m.start(), m.end())}».")

    if a.get("danda"):
        script = _INDIC_BN if lang == "bn" else _INDIC_DEV
        m = re.search(rf"[{script}]\.(?=\s|$)", t)
        if m:
            add(f"Точка в конце предложения — нужна данда «।»: «{_snip(t, m.start(), m.end())}».")
        m = re.search(r"\d।", t)
        if m:
            add(f"После числа в конце предложения нужна точка «.», а не «।» (её путают с 1): «{_snip(t, m.start(), m.end())}».")

    cjk = a.get("cjk_punct") or ""
    if cjk == "ja":
        m = re.search(rf"[{_JA}][,.](?!\d)", t)
        if m:
            add(f"Латинская запятая/точка в японском тексте — нужны «、» и «。»: «{_snip(t, m.start(), m.end())}».")
        m = re.search(r"[。、] ", t)
        if m:
            add(f"Пробел после «。»/«、» — в японском пробелы не ставятся: «{_snip(t, m.start(), m.end())}».")
    if cjk == "zh":
        m = re.search(rf"[{_HAN}][,.!?:;](?!\d)|[,!?:;](?=[{_HAN}])", t)
        if m:
            add(f"Полуширинный знак препинания в китайском тексте — нужны полноширинные «，。！？：；»: «{_snip(t, m.start(), m.end())}».")
        m = re.search(rf"\([^()]*[{_HAN}][^()]*\)", t)
        if m:
            add(f"Полуширинные скобки вокруг китайского текста — нужны «（）»: «{_snip(t, m.start(), m.end())}».")
        m = re.search(rf"（[^（）{_HAN}]*）", t)
        if m and re.search(r"[A-Za-z0-9]", m.group(0)):
            add(f"Полноширинные скобки вокруг латиницы/цифр — нужны «()»: «{_snip(t, m.start(), m.end())}».")
    if a.get("zh_latin_space"):
        m = re.search(rf"[{_HAN}][A-Za-z0-9]|[A-Za-z0-9][{_HAN}]", t)
        if m:
            add(f"Нет пробела между иероглифами и латиницей/цифрами: «{_snip(t, m.start(), m.end())}».")

    if a.get("es_opening_marks"):
        for close, open_, name in (("!", "¡", "восклицательного"), ("?", "¿", "вопросительного")):
            if t.count(close) > t.count(open_):
                add(f"Нет открывающего {name} знака «{open_}» — в испанском нужны оба: {open_}…{close}")
        m = re.search(r"[¡¿]\s|\s[!?]", t)
        if m:
            add(f"Пробел внутри ¡…! / ¿…?: «{_snip(t, m.start(), m.end())}».")

    if a.get("fr_spaces"):
        for m in re.finditer(r"[;:!?]", t):
            i = m.start()
            prev = t[i - 1] if i > 0 else ""
            nxt = t[i + 1] if i + 1 < len(t) else ""
            if m.group() == ":" and prev.isdigit() and nxt.isdigit():
                continue
            if prev and not prev.isspace() and prev not in "!?;:" and prev != _MASK:
                add(f"Перед «{m.group()}» нужен пробел: «{_snip(t, i, i + 1)}».")
            if nxt and not nxt.isspace() and nxt not in "!?;:»)\"”" and nxt != _MASK:
                add(f"После «{m.group()}» нужен пробел: «{_snip(t, i, i + 1)}».")
        m = re.search(r"«(?!\s)|(?<!\s)»", t)
        if m:
            add(f"Внутри кавычек « » нужны пробелы: «{_snip(t, m.start(), m.end())}».")

    if a.get("no_final_period"):
        m = re.search(rf"[{_THAI}]\.(?=\s|$)", t)
        if m:
            add(f"Точка в конце предложения — в тайском не ставится: «{_snip(t, m.start(), m.end())}».")

    if a.get("no_period_after_currency"):
        m = re.search(r"(?:грн|UAH)\.", t)
        if m:
            add(f"Точка после «грн»/«UAH» не ставится: «{_snip(t, m.start(), m.end())}».")

    # Ordinals: checked only when an ending is actually written.
    if a.get("ordinals", True) and lang not in ("en",):
        pats = [_ORD_EN] + ORDINAL_PATTERNS.get(lang, [])
        for rx, right in pats:
            frags = []
            for m in rx.finditer(t):
                frag = m.group(0).strip()
                if frag not in frags:
                    frags.append(frag)
            if frags:
                shown = ", ".join(f"«{x}»" for x in frags)
                word = "Порядковое числительное" if len(frags) == 1 else "Порядковые числительные"
                add(f"{word} {shown} — оформлено не по стайлгайду: {right}.")

    if a.get("fs_fb_latin", True) and lang != "ko":
        m = _FS_FB_BAD.search(t)
        if m:
            add(f"Сокращение «{m.group(0)}» — по стайлгайду только латиницей «FS»/«FB».", "medium")

    for item in ((rules.get("terms") or {}).get("items") or []):
        bad = (item.get("bad") or "").strip()
        if not bad:
            continue
        if _term_regex(bad, lang).search(t):
            good = (item.get("good") or "").strip()
            note = (item.get("note") or "").strip()
            msg = f"Запрещённый вариант «{bad}»" + (f" — нужно «{good}»" if good else "") + (f" ({note})" if note else "") + "."
            add(msg, "medium")
    return out


def _ranges_signs(t: str) -> set[str]:
    return {m.group(3) for m in _RANGE_RE.finditer(_mask(t)) if m.group(3) in "-–"}


def check_document(rows: list[tuple[int, str]], rules: dict | None, lang: str) -> dict[int, list[dict]]:
    """Document-wide consistency for one language: one kind of quotes, one
    kind of ellipsis, one range dash. Rows using a minority variant get a
    finding. rows = [(excel_row, translation)]."""
    a = auto_of(rules)
    if not rules or not a.get("consistency", True):
        return {}
    out: dict[int, list[dict]] = {}

    def minority(per_row: dict[int, set[str]], label, describe):
        counts: dict[str, int] = {}
        for kinds in per_row.values():
            for k in kinds:
                counts[k] = counts.get(k, 0) + 1
        if len(counts) < 2:
            return
        major = max(counts, key=lambda k: (counts[k], k))
        for r, kinds in per_row.items():
            other = kinds - {major}
            if other:
                msg = (f"Разнобой в файле ({label}): здесь {', '.join(describe(k) for k in sorted(other))}, "
                       f"а в большинстве строк {describe(major)} — нужно единообразно.")
                out.setdefault(r, []).append(_f(msg))

    allowed = set(a.get("quotes") or [])
    nested = _NESTED_OK.get(lang, set())
    q = {}
    for r, t in rows:
        kinds = quote_kinds(t)
        if allowed:
            kinds &= allowed
        if nested and kinds - nested:
            kinds = kinds - nested
        if kinds:
            q[r] = kinds
    minority(q, "кавычки", lambda k: QUOTE_KINDS.get(k, k))

    if not a.get("ellipsis_char"):
        e = {}
        for r, t in rows:
            mt = _mask(t)
            kinds = set()
            if "..." in mt:
                kinds.add("...")
            if "…" in mt:
                kinds.add("…")
            if kinds:
                e[r] = kinds
        minority(e, "многоточие", lambda k: f"«{k}»")

    if (a.get("ranges") or "any") in ("any", "en_or_hyphen"):
        g = {r: s for r, t in rows if (s := _ranges_signs(t))}
        minority(g, "тире в диапазонах", lambda k: "дефис «-»" if k == "-" else "тире «–»")
    return out


def section_is_valid(section: str, value) -> bool:
    if section not in SECTION_KEYS or not isinstance(value, dict):
        return False
    if section == "terms":
        items = value.get("items")
        return isinstance(items, list) and all(isinstance(i, dict) for i in items)
    if section == "auto":
        return True
    if section == "tone":
        return value.get("level", "") in TONE_LEVELS
    return isinstance(value.get("text", ""), str)


def clean_section(section: str, value: dict) -> dict:
    if section == "terms":
        items = []
        for i in value.get("items") or []:
            bad = str(i.get("bad") or "").strip()
            if bad:
                items.append({"bad": bad, "good": str(i.get("good") or "").strip(), "note": str(i.get("note") or "").strip()})
        return {"items": items}
    if section == "auto":
        out = dict(AUTO_DEFAULT)
        for k, default in AUTO_DEFAULT.items():
            if k not in value:
                continue
            v = value[k]
            if isinstance(default, bool):
                out[k] = bool(v)
            elif isinstance(default, list):
                out[k] = [x for x in (v or []) if x in QUOTE_KINDS]
            else:
                out[k] = str(v or "")
        if out["em_dash"] not in EM_DASH_MODES:
            out["em_dash"] = "any"
        if out["ranges"] not in RANGE_SIGNS:
            out["ranges"] = "any"
        if out["range_spaces"] not in RANGE_SPACES:
            out["range_spaces"] = "any"
        if out["cjk_punct"] not in ("", "ja", "zh"):
            out["cjk_punct"] = ""
        return out
    if section == "tone":
        return {
            "level": str(value.get("level") or ""),
            "form": str(value.get("form") or "").strip(),
            "avoid": str(value.get("avoid") or "").strip(),
            "note": str(value.get("note") or "").strip(),
        }
    return {"text": str(value.get("text") or "").strip()}


def meta() -> dict:
    """Everything the settings screen needs to draw itself."""
    return {
        "langs": [{"key": k, "label": label} for k, label, _ in LANGS],
        "sections": [{"key": k, "title": t, "kind": kind} for k, t, kind in SECTIONS],
        "tone_levels": TONE_LEVELS,
        "quote_kinds": QUOTE_KINDS,
        "range_signs": RANGE_SIGNS,
        "range_spaces": RANGE_SPACES,
        "em_dash_modes": EM_DASH_MODES,
        "auto_default": AUTO_DEFAULT,
    }


# ----------------------------------------------------------- other clients --
# 2026-10-05 (Александр): more clients. Play Fortuna, Olymp, PD — betting &
# gambling like 1win, no styleguide of their own: they follow 1win's rules
# for now, but with NO tone requirement (the report only states which tone
# is used). Айтыс — an advertising agency, general marketing: no styleguide.
# «Остальное» — one-off jobs for other projects.
OTHER_CLIENTS: list[dict] = [
    {"name": "Play Fortuna", "domain": "betting", "copy_1win": True,
     "projects": [("Play Fortuna", ["fi-fi", "pt-br", "pt-pt", "pl-pl", "es-es", "es-mx", "es-ar",
                                    "fr-fr", "fr-ca", "kk-kz", "de-de"])]},
    {"name": "PD", "domain": "betting", "copy_1win": True,
     "projects": [("PD", ["kk-kz", "uz-uz", "az-az"])]},
    {"name": "Olymp", "domain": "betting", "copy_1win": True,
     "projects": [("Olymp", ["en", "az-az", "kk-kz", "ky-kg"])]},
    {"name": "Айтыс", "domain": "marketing", "copy_1win": False,
     "projects": [("Айтыс", ["kk-kz", "ky-kg", "uz-uz", "tg-tj", "ru-ru"])]},
    {"name": "Остальное", "domain": "", "copy_1win": False,
     "projects": [("Разовые задачи", sorted(set(_ALL_TOV) | {"en", "fi-fi", "fr-ca"}))]},
]


def without_tone(styleguide: dict | None) -> dict:
    """A copy of a styleguide with every language's tone section removed."""
    out = {}
    for lang, secs in (styleguide or {}).items():
        rest = {k: copy.deepcopy(v) for k, v in (secs or {}).items() if k != "tone"}
        if rest:
            out[lang] = rest
    return out
