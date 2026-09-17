import json
import re

import httpx

from app.config import settings

# "register" (tone of address) is NOT in here — as of 2026-09-16 it isn't
# an error-finding check at all any more, so it never appears in the
# "Что проверять" problem list _checks_description builds from this dict.
# It used to require Александр to upload a "Тон обращения" document naming
# the correct formal/informal register per language, and flag a violation
# against it — but that meant the check could be structurally blind to a
# translator using the SAME wrong register in every single row (uniform
# ≠ correct, but a rule that only looks for internal disagreement can't
# tell the two apart). Александр asked to drop the whole document and
# have the check report the register actually used instead of judging it
# — see REGISTER_VALUE_TYPE, _register_instructions, and
# summarize_register_values below for the replacement, and
# _allowed_ai_types for how "register_value" gets recognized as a valid
# response type only when "register" is one of the selected checks.
CHECK_LABELS = {
    # (A "glossary" check used to live here too — required-term matching
    # against an uploaded glossary document. Removed: unlike the other AI
    # checks, that one never actually needed a probabilistic model — a term
    # either matches the glossary or it doesn't, a plain text comparison —
    # so it kept missing/mislabeling things for no good reason. See git
    # history for the removal.)
    "typo": (
        "опечатки/ошибки — это ДВЕ разные вещи, обе входят сюда: (1) обычные опечатки и орфографические ошибки в "
        "самом переводе — неправильно написанное слово, даже если смысл всё равно понятен из контекста (например "
        "«resulits» вместо «results») — это опечатка, и её нужно найти; (2) ошибки, искажающие смысл (пропущенное "
        "отрицание, спутанные число/род, потеря смысла, грамматика, ломающая понимание). Не путай это со СТИЛЕМ: "
        "другой синоним с тем же смыслом, другой порядок слов, другая, но тоже корректная формулировка — это НЕ "
        "опечатка и не ошибка, о таком сообщать не нужно. Сюда же относится ДРУГАЯ ВАЛЮТА, чем в исходнике "
        "(например, евро вместо доллара, или другой ISO-код) — это меняет смысл суммы, а не просто стиль"
    ),
    "untranslatable": (
        "непереводимые термины — имена турниров/событий/игр/брендов/продуктов/акций, а также устоявшиеся "
        "маркетинговые слова, которые обычно оставляют как есть (например «VIP», «Lootbox», название турнира вроде "
        "«Grand Prix»). Сильный сигнал, что термин непереводимый: если он уже в САМОМ ИСХОДНИКЕ оставлен нетронутым "
        "(написан на другом языке/латиницей внутри текста на другом языке/скрипте) — значит, почти наверняка он "
        "должен остаться таким же нетронутым и в переводе, В СВОЁМ ИСХОДНОМ НАПИСАНИИ (тем же алфавитом/письменностью, "
        "что и в исходнике). Сообщай находку, если термин в переводе реально ИЗМЕНЁН: переведён по смыслу, искажён, "
        "пропущен, ИЛИ полностью переписан другими буквами другого алфавита по звучанию — например «Elite & Fortune» "
        "→ «엘리트 & 포춘» хангылем, «Grand Prix» → «Гран При» кириллицей вместо латиницы. Такая транслитерация в "
        "ДРУГОЙ алфавит/письменность — это ВСЕГДА ошибка, даже если сделана одинаково и последовательно во всём "
        "документе: имя турнира/бренда должно оставаться в исходном написании без исключений. Единственное "
        "исключение — падежное/грамматическое окончание, добавленное К ТЕРМИНУ, ОСТАВЛЕННОМУ В СВОЁМ ИСХОДНОМ "
        "НАПИСАНИИ (например «Grand Prix'а», «iPhone'ов» — сам термин не тронут и не переписан другим алфавитом, "
        "просто добавлено окончание по грамматике целевого языка): это НЕ ошибка. Если термин в переводе остался "
        "ровно как в исходнике, в своём исходном написании (тем же алфавитом, что и в оригинале, при необходимости — "
        "с окончанием по грамматике целевого языка) — это ПРАВИЛЬНО, находки быть не должно, даже если может "
        "показаться, что его \"следовало\" перевести — не сообщай о том, что и так сделано верно. При конфликте с "
        "«Особыми указаниями» ниже — следуй им"
    ),
    "completeness": (
        "неполнота перевода — ЛЮБОЙ случай, когда содержательный кусок исходного текста не дошёл до перевода: (1) "
        "обычные слова/фраза/предложение по ОШИБКЕ остались НЕПЕРЕВЕДЁННЫМИ, просто скопированы внутри перевода как "
        "есть, хотя должны были быть переведены (это НЕ относится к отдельным именам/брендам/терминам, которые "
        "правильно оставлены нетронутыми намеренно — за них отвечает отдельная проверка «непереводимые термины», "
        "и там это не находка); (2) весь перевод "
        "сделан на другом языке, чем требуемый целевой (например, вставлен не тот язык, или перевод не изменился с "
        "другого родственного языка); (3) целое предложение, пункт списка или значимый смысловой кусок ПРОПУЩЕН из "
        "перевода целиком — просто отсутствует в переводе в каком бы то ни было виде. Пункт (3) — это НЕ то же самое, "
        "что естественное опущение одного-двух слов ради благозвучия (артикль, вводное слово, лёгкая перестройка "
        "фразы) — такое нормально и не считается находкой; флагуй именно когда пропадает целый КУСОК СМЫСЛА — целое "
        "предложение, целый пункт правил, значимая часть информации, которую читатель перевода вообще не увидит. Не "
        "путать с пустым переводом (отдельная проверка) или с иной длиной перевода — сама по себе длина не проблема; "
        "(4) в исходнике есть символ-стрелка для перехода/призыва к действию (например «->», «→», «=>» — так в "
        "рассылках обычно оформляют переход по ссылке или кнопку), а в переводе он пропал целиком ИЛИ искажён "
        "(например, разбит пробелом там, где в исходнике его не было, направлен в другую сторону, заменён на "
        "другой символ) — сообщай об этом как о находке по этому же критерию, даже если весь остальной текст "
        "переведён правильно; если стрелка в переводе осталась ровно как в исходнике — находки быть не должно"
    ),
}

CALIBRATION_BASE = (
    "Общее правило: сообщай, только если уверен(а), что это настоящая ошибка. Сомневаешься или это может быть "
    "допустимым вариантом — не включай. Лучше меньше, но точных находок. Порядок символа валюты относительно числа, "
    "разделители тысяч/десятичных знаков, а также сам порядок частей даты (день/месяц/год) и то, точкой или "
    "слэшем они разделены — это НЕ ошибка перевода сама по себе, и об этом никогда не нужно сообщать. Важно: одна "
    "пара «исходник/перевод» может содержать НЕСКОЛЬКО разных проблем одновременно, в том числе разных типов из "
    "списка ниже — не останавливайся после первой найденной в паре ошибки, полностью проверь пару на КАЖДЫЙ "
    "выбранный критерий и включи в ответ отдельную запись на каждую отдельную настоящую находку, даже если несколько "
    "находок относятся к одной и той же паре. Это касается и повторов ВНУТРИ одной и той же пары: если исходник или "
    "перевод — это большой фрагмент из нескольких предложений (например, содержимое одной ячейки таблицы целым "
    "абзацем), и одна и та же по сути проблема реально встречается в нескольких РАЗНЫХ предложениях/местах этого "
    "текста — не сворачивай это в одну общую находку на весь текст пары. Включи отдельную находку на КАЖДОЕ отдельное "
    "предложение/фрагмент, где проблема реально встречается (даже если их 10, 20 или больше), и обязательно укажи в "
    "сообщении каждой находки, к какому именно предложению/фрагменту она относится (например, процитируй именно его) "
    "— чтобы находки не выглядели одинаковыми и не терялись друг в друге."
)

# When "numbers" is also running (a free, 100%-reliable rule check — see
# app.rule_checks.check_numbers — auto-included whenever "Оформление" is
# selected), it already catches every plain digit/date mismatch on its own.
# Telling the AI to still report those under "опечатки/ошибки" too just
# duplicates the same finding twice under two different labels — Александр
# hit exactly this (a wrong year in a date, shown once as "numbers" and
# again, reworded, as "typo"). So when it's running alongside, the AI is
# told to leave plain digits to it and only flag currency IDENTITY (a
# symbol/code that doesn't match — not itself a digit, so "numbers" can't
# catch it). When "numbers" ISN'T selected for this run, the AI keeps
# acting as the only backstop for a wrong number/date, exactly as before.
_CALIBRATION_WITH_NUMBERS_CHECK = (
    "Расхождения в самих цифрах (неверное число, неверная дата и т.п.) уже ловит отдельная бесплатная "
    "автоматическая проверка чисел, включённая в эту проверку — не сообщай о них здесь, даже если заметишь; "
    "в «опечатки/ошибки» сообщай только о несовпадении самой валюты (символ или код, например евро вместо "
    "доллара), а не о цифрах."
)
_CALIBRATION_WITHOUT_NUMBERS_CHECK = (
    "настоящая ошибка — это когда сама валюта или число не совпадают с исходником по смыслу "
    "(см. «опечатки/ошибки»), а не то, как они оформлены."
)


def _calibration(checks: list[str]) -> str:
    tail = _CALIBRATION_WITH_NUMBERS_CHECK if "numbers" in checks else _CALIBRATION_WITHOUT_NUMBERS_CHECK
    return f"{CALIBRATION_BASE} {tail}"


SINGLE_PROMPT = """Ты — модуль контроля качества перевода для бюро переводов. Даны исходный текст и перевод.
Проверяй только критерии из "Что проверять" ниже.

{target_lang_line}

{calibration}

{source_lang_note}

Исходный текст:
\"\"\"{source}\"\"\"

Перевод:
\"\"\"{translation}\"\"\"

Особые указания к задаче (важнее общих правил, если есть):
{extra_instructions}

Что проверять: {checks_description}
Даже если заметишь другую проблему вне этого списка (в т.ч. очевидную и серьёзную) — не включай её в ответ вообще,
ни под каким из перечисленных типов; для неё есть отдельная проверка, которую нужно включить отдельно. Не подгоняй
такую находку под ближайший по смыслу разрешённый тип только потому, что это единственный доступный вариант —
если находка не является настоящим примером именно этого критерия, её не должно быть в ответе.
{register_instructions}
Верни ТОЛЬКО валидный JSON-массив без markdown и пояснений, строго в этой форме
(пустой массив [], если проблем нет{register_array_note}):
[
  {{"type": "{type_enum}", "severity": "low|medium|high", "message": "конкретное описание на русском, с указанием места в тексте, если уместно"}}
]"""

BATCH_PROMPT = """Ты — модуль контроля качества перевода для бюро переводов. Даны пары (контекст, исходный текст, перевод) на один целевой язык.
Проверяй только критерии из "Что проверять" ниже. По умолчанию оценивай каждую пару отдельно от остальных — но если
описание конкретного критерия ниже прямо просит сравнить пары между собой, следуй этому описанию для этого критерия.

{target_lang_line}

{calibration}

{source_lang_note}

Особые указания к задаче (важнее общих правил, если есть):
{extra_instructions}

Что проверять: {checks_description}
Даже если заметишь другую проблему вне этого списка (в т.ч. очевидную и серьёзную) — не включай её в ответ вообще,
ни под каким из перечисленных типов; для неё есть отдельная проверка, которую нужно включить отдельно. Не подгоняй
такую находку под ближайший по смыслу разрешённый тип только потому, что это единственный доступный вариант —
если находка не является настоящим примером именно этого критерия, её не должно быть в ответе.

Отдельно — про повторяющиеся ошибки (это НЕ противоречит правилу "оценивай каждую пару отдельно" выше: ты всё равно
оцениваешь и находишь проблему в каждой паре независимо, а правило ниже только про то, как ОФОРМИТЬ ответ, если
независимая оценка нескольких разных пар дала одну и ту же находку): если ты видишь, что у тебя есть весь список пар
целиком, и ОДНА И ТА ЖЕ конкретная проблема (не просто похожий тип, а именно тот же самый термин/фраза/ошибка)
встречается одинаково в НЕСКОЛЬКИХ парах — не повторяй её отдельной находкой на каждую пару. Вместо этого включи её
ОДИН раз в форме {{"rows": [номера всех пар, где встречается], ...}} (вместо "row") с сообщением, начинающимся с
"Повторяется по всему документу: " и описанием самой проблемы. Если проблема встречается не во всех повторениях
одного и того же (где-то переведено правильно, где-то нет) — перечисли в "rows" только те пары, где она РЕАЛЬНО есть,
и явно скажи в сообщении, что не везде одинаково; но если после такого отбора остаётся только ОДНА пара — это уже не
повторение, а обычная одиночная находка, оформи её как "row", без формулировки "Повторяется по всему документу".
Если сомневаешься, что это действительно одна и та же проблема, а не просто похожая — сообщай как обычно, отдельными
находками с "row". Важное отличие: всё это — про повтор одной и той же проблемы в РАЗНЫХ парах (разные номера в
списке ниже). Если же несколько повторов одной и той же проблемы находятся ВНУТРИ одной и той же пары (например,
несколько предложений в одной длинной ячейке, и в каждом — та же самая проблема) — это НЕ про "rows" и не про
повтор по документу вообще: сообщай о каждом таком повторе как об обычной отдельной находке с "row" на этот же
номер пары (см. общее правило про повторы внутри одной пары выше), а не объединяй их в одну находку и не пытайся
запихнуть один и тот же номер пары в "rows" несколько раз.

Важно про сам текст "message": НИКОГДА не упоминай в нём номер пары/строки — ни словом ("пара 2", "строка 5"), ни
просто числом в скобках. Эти номера из списка "Пары для проверки" ниже существуют только внутри этого запроса и НЕ
совпадают с реальными номерами строк в файле, которые видит менеджер — их подстановкой в итоговый отчёт занимается
сама программа (через "row"/"rows" и, для повторов, автоматическую пометку вида "также в строках: …", которую ты
не пишешь сам). Если нужно различить конкретные места — используй ТОЛЬКО цитаты самого текста (например, конкретную
фразу или предложение из перевода), а не номера пар.

Пары для проверки:
{pairs_block}
{register_instructions}
Верни ТОЛЬКО валидный JSON-массив по всем парам без markdown и пояснений, строго в этой форме
(пустой массив [], если нигде нет обычных находок; не включай пары без обычных находок{register_array_note}):
[
  {{"row": <номер пары из списка выше>, "type": "{type_enum}", "severity": "low|medium|high", "message": "конкретное описание на русском, с цитатой конкретного предложения/фрагмента, если в паре их несколько — без номеров пар/строк внутри самого текста message"}},
  {{"rows": [<номера ВСЕХ пар, где повторяется одна и та же проблема>], "type": "{type_enum}", "severity": "low|medium|high", "message": "Повторяется по всему документу: ..."}}
]
(используй "rows" вместо "row" ТОЛЬКО для настоящего повторения одной и той же проблемы в нескольких парах — см.
выше; для обычной, отдельной находки в одной паре используй "row" как всегда)"""


def _source_lang_note(source_lang: str, checks: list[str] | None = None) -> str:
    """Client-specific rule: when the source is Russian, English words or
    phrases embedded in it (brand names, terms, rare exceptions aside)
    should stay in English in every target translation too — not be
    translated into the target language.

    Which check-type a violation is filed under depends on what's actually
    selected: "untranslatable" (see CHECK_LABELS) is the more specific,
    natural home for exactly this pattern — an English brand/term/event
    name left untranslated in a Russian source is usually the same thing
    CHECK_LABELS["untranslatable"] already asks about — so defer to it
    when it's part of this run, and only fall back to "неполнота перевода"
    when "untranslatable" isn't selected at all. Without this, a run with
    BOTH checks selected (the common case — both default on) could tell
    the model two different, contradictory things about the identical
    pattern in the same prompt."""
    if source_lang.strip().lower() != "ru":
        return ""
    category = "непереводимые термины" if checks and "untranslatable" in checks else "неполнота перевода"
    return (
        "Особое правило: если в русском исходнике есть слова или фразы на английском (не считая редких "
        "исключений), они должны остаться на английском и в переводе на другой язык — не переводиться. Если такой "
        f"фрагмент всё же переведён на язык перевода, это ошибка (относи к «{category}»)."
    )



# Some client files label a language column with a code that doesn't match
# its real ISO-639 meaning. Most notably "my" — ISO-639-1 defines that as
# Burmese (Myanmar), but Александр's exports use it for Malay (short for
# "Malaysia"). Left to its own knowledge of the ISO standard, the model
# assumes Burmese, expects Burmese script, and then reports the actual
# (correct) Malay text as being in the wrong language. Overriding this one
# code's meaning in the prompt fixes it regardless of which convention the
# model would otherwise guess.
LANG_CODE_MEANING_OVERRIDES = {
    "my": "малайский (Malay, Малайзия) — а НЕ бирманский/мьянманский, хотя по стандарту ISO 639 код «my» формально означает бирманский",
}


def _target_lang_line(target_lang: str) -> str:
    """Explicitly names the target language rather than leaving the model
    to infer it purely from the translated text — closely related
    languages (e.g. Turkish/Azerbaijani, Kazakh/Kyrgyz) are otherwise a
    real risk of being mixed up, especially in short texts."""
    code = target_lang.strip().lower()
    if not code:
        return ""
    override = LANG_CODE_MEANING_OVERRIDES.get(code.split("-")[0])
    if override:
        return (
            f"Целевой язык перевода обозначен кодом «{code}», но здесь этот код означает: {override}. "
            "Ориентируйся именно на этот язык, а не на формальное значение кода по стандарту ISO."
        )
    return f"Целевой язык перевода: {code}. Ориентируйся конкретно на этот язык — не путай с родственными языками."


def _checks_description(checks: list[str]) -> str | None:
    """The "Что проверять: ..." problem list — deliberately unaffected by
    "register", which was removed from CHECK_LABELS entirely on 2026-09-16
    (see that dict's own comment) and is instead handled by
    _register_instructions/_register_array_note below, as a completely
    separate, clearly-delineated task ("report what's there", not "find
    what's wrong") rather than one more entry in this problem list."""
    ai_checks = [c for c in checks if c in CHECK_LABELS]
    if not ai_checks:
        return None
    return "; ".join(CHECK_LABELS[c] for c in ai_checks) or None


# The "register" (tone of address) response entries are never a "problem" —
# see CHECK_LABELS's comment for why this replaced the old formal/informal
# rule-and-violation design on 2026-09-16. REGISTER_VALUE_TYPE is the
# "type" the model uses for these entries so downstream code
# (_allowed_ai_types here; the extraction helpers in app.excel_multi and
# in run_ai_checks below) can tell them apart from a real finding and
# route them to the report instead of the visible findings list.
REGISTER_VALUE_TYPE = "register_value"

# Unlike formal/informal/neutral above, "mixed" (the model reports it when
# ONE row's translation itself switches between «ты» and «вы» instead of
# using one consistently — Александр's ask, 2026-09-17: a single cell can
# hold several sentences/paragraphs, and the tone can genuinely drift
# mid-cell) IS a real problem worth the manager's attention — internal
# inconsistency within one string, not a matter of which tone the
# document as a whole should use. So it's never folded into
# build_register_report's majority/exception counting (a "mixed" row is
# neither a vote for the majority nor a counted exception) — instead
# _register_mixed_finding() below turns it into an ordinary visible
# finding on that exact row, synthesized entirely on our side (the model
# only ever needs to report the plain value; it never has to also invent
# a second, separate finding for the same thing).
REGISTER_MIXED_TYPE = "register_mixed"


def _register_mixed_finding() -> dict:
    return {
        "type": REGISTER_MIXED_TYPE,
        "severity": "medium",
        "message": (
            "В этой строке смешаны разные формы обращения к пользователю — где-то «вы», где-то «ты» — "
            "внутри одного и того же текста. Проверьте, не разошёлся ли тон посреди фразы."
        ),
    }

# Languages with no grammatical formal/informal distinction in the word
# for "you" at all (English's single "you" being Александр's own example,
# 2026-09-17) — for these, asking the model to classify "вы"/"ты" has
# nothing real to go on, and would otherwise have it guessing a tone from
# indirect style cues (word choice, "please", contractions) instead of an
# actual grammatical marker, producing an unreliable pseudo-tone. Rather
# than have the model try (and build_register_report show a shaky
# result), the register instructions are skipped ENTIRELY for these
# languages — no register_value entries are ever asked for or returned,
# so no register report is built at all, exactly as if "register" hadn't
# been selected for that language.
#
# Deliberately conservative: only a language actually confirmed to lack
# this distinction belongs here. Many languages that might look similar at
# a glance still do have a real marker (German du/Sie, Spanish tú/usted,
# Turkish sen/siz, Hindi tu/tum/aap, Chinese 你/您, ...) — those are left
# to the model, which handles them well. Add another base language code
# here only once actually confirmed to have no such distinction at all.
NO_REGISTER_DISTINCTION_LANGS = {"en"}


def _lacks_register_distinction(target_lang: str) -> bool:
    return target_lang.strip().lower().split("-")[0] in NO_REGISTER_DISTINCTION_LANGS


# Per-language corrections for a specific way the model can misjudge the
# formal/informal call above — NOT a "no distinction" case (register
# instructions still run in full), just a nudge on ONE surface trap that's
# confirmed to trip the model up for that exact language variant.
#
# Александр's concrete case (2026-09-17, pt-BR promo/bot text): Brazilian
# Portuguese "você" grammatically conjugates like a third-person pronoun —
# superficially the same shape as Spanish "usted" or French "vous", which
# really ARE the formal register in those languages — but in everyday
# Brazilian usage "você" is the ORDINARY, default address, the equivalent
# of «ты», not «вы». The genuinely formal Portuguese address is "o
# senhor"/"a senhora". Without a nudge, the model leaned on that surface
# resemblance and called a "você" text "formal". Deliberately scoped to
# "pt-br" alone (the exact normalized code from parse_workbook, always
# lowercase "xx-yy") — European Portuguese (pt-PT) leans the other way
# (there "você" reads more formal, "tu" is the informal one) and must NOT
# get this same hint.
REGISTER_LANGUAGE_HINTS: dict[str, str] = {
    "pt-br": (
        'Важное уточнение для бразильского португальского (pt-BR): местоимение "você" — это '
        'ОБЫЧНОЕ, нейтральное обращение уровня «ты», а НЕ форма на «вы», даже хотя глагол при нём '
        'формально спрягается как в третьем лице (это может визуально напомнить испанское "usted" '
        'или французское "vous" — но в Бразилии на практике "você" используется в быту, рекламе и '
        'обращениях к клиенту точно так же часто и просто, как «ты» по-русски). Настоящее формальное '
        'обращение на «вы» в португальском — это "o senhor"/"a senhora". Не считай сам факт '
        'использования "você" признаком формального регистра.\n'
    ),
}


def _register_language_hint(target_lang: str) -> str:
    # "_" -> "-" defensively: parse_workbook's own normalization always
    # produces a hyphen, but this same target_lang also reaches here raw
    # (never normalized at all) from the standalone /check endpoint, and a
    # manager-taught language alias isn't required to use a hyphen either
    # — so a stray underscore spelling of "pt-br" shouldn't silently miss
    # this lookup and let the original misjudgment back in.
    key = target_lang.strip().lower().replace("_", "-")
    return REGISTER_LANGUAGE_HINTS.get(key, "")


def _register_instructions(checks: list[str], batch: bool, target_lang: str = "") -> str:
    """Empty string when "register" isn't selected, or when target_lang is
    one of NO_REGISTER_DISTINCTION_LANGS above (nothing added to the
    prompt at all either way). Otherwise, a clearly separate paragraph —
    deliberately NOT folded into the "Что проверять" problem list
    _checks_description builds — asking the model to classify the
    register actually used, for every pair, regardless of whether it's
    "correct": this is information-gathering, not error-detection, so it
    must never be described to the model as a problem to avoid or a
    mistake to flag.

    batch=True (BATCH_PROMPT, several pairs of one language visible
    together) asks for one entry per pair, tagged by row number, matching
    that prompt's existing "row" numbering. batch=False (SINGLE_PROMPT,
    exactly one pair — the standalone /check endpoint) asks for exactly
    one entry with no row number, since that prompt's own findings don't
    carry one either.

    Also appends _register_language_hint(target_lang) — normally empty,
    but a short language-specific correction for the rare case where the
    model tends to misjudge THIS particular language's own formal marker
    (see REGISTER_LANGUAGE_HINTS)."""
    if "register" not in checks or _lacks_register_distinction(target_lang):
        return ""
    if batch:
        return (
            "\nОтдельная задача, НЕ связанная с находками выше — не поиск ошибки, а сбор информации о том, "
            "как переведено на самом деле: добавь в тот же JSON-массив ОДНУ дополнительную запись на КАЖДУЮ "
            "пару из списка «Пары для проверки» выше, даже если для неё нет ни одной обычной находки, "
            f'строго в форме {{"row": <номер пары>, "type": "{REGISTER_VALUE_TYPE}", "severity": "low", '
            '"value": "formal|informal|neutral|mixed", "message": ""} — value: "formal", если в ПЕРЕВОДЕ этой '
            'пары использовано обращение на «вы» (или аналог для этого языка); "informal", если на «ты»; '
            '"mixed", если В ПРЕДЕЛАХ ЭТОЙ ОДНОЙ пары (перевод может состоять из нескольких предложений или '
            'абзацев в одной ячейке) обращение к пользователю НЕПОСЛЕДОВАТЕЛЬНО — где-то встречается «вы», а '
            'где-то «ты», а не одна форма единообразно на протяжении всего текста пары; "neutral", если в '
            'переводе этой конкретной пары нет прямого обращения к пользователю вообще (например, только '
            'название, число, техническая метка) — тогда не угадывай по смыслу, отвечай "neutral". Это НЕ '
            "находка об ошибке — не описывай её как проблему, не оценивай, правильная это форма или нет, "
            "просто зафиксируй, что реально написано в переводе (кроме значения \"mixed\" — это описание "
            "реального факта смешения форм внутри одной ячейки, а не оценка).\n"
        ) + _register_language_hint(target_lang)
    return (
        "\nОтдельная задача, НЕ связанная с находками выше — не поиск ошибки, а сбор информации о том, как "
        "переведено на самом деле: добавь в тот же JSON-массив ОДНУ дополнительную запись, строго в форме "
        f'{{"type": "{REGISTER_VALUE_TYPE}", "severity": "low", "value": "formal|informal|neutral|mixed", '
        '"message": ""} — value: "formal", если в переводе использовано обращение на «вы» (или аналог для '
        'этого языка); "informal", если на «ты»; "mixed", если в пределах ЭТОГО ОДНОГО перевода (он может '
        'состоять из нескольких предложений или абзацев) обращение к пользователю непоследовательно — где-то '
        'встречается «вы», а где-то «ты», а не одна форма единообразно на протяжении всего текста; "neutral", '
        'если в переводе нет прямого обращения к пользователю вообще — тогда не угадывай по смыслу, отвечай '
        '"neutral". Это НЕ находка об ошибке — не описывай её как проблему, не оценивай, правильная это форма '
        "или нет, просто зафиксируй, что реально написано в переводе (кроме значения \"mixed\" — это описание "
        "реального факта смешения форм внутри одного текста, а не оценка).\n"
    ) + _register_language_hint(target_lang)


def _register_array_note(checks: list[str], target_lang: str = "") -> str:
    """Appended to the "(пустой массив [] ...)" output-format line so it
    stays true once _register_instructions adds its own mandatory entries
    — without this, "пустой массив, если проблем нет" would directly
    contradict "add one entry per pair regardless" a few lines above it.
    Mirrors _register_instructions' own no-distinction-language skip (see
    NO_REGISTER_DISTINCTION_LANGS) — when no register instructions were
    actually added to the prompt, this note has nothing to justify and
    must stay empty too."""
    if "register" not in checks or _lacks_register_distinction(target_lang):
        return ""
    return " — но если выбран регистр обращения, эти дополнительные записи всё равно обязательны"


# Александр's own cutoff for when showing each exception row's actual text
# (see build_register_report below) stops being more useful than just
# naming the rows.
MAX_EXCEPTIONS_WITH_TEXT = 3


def build_register_report(values: dict, texts: dict | None = None, single: bool = False) -> dict | None:
    """values: {label: "formal"|"informal"|"neutral"}, one entry per
    classified row — label is whatever the caller uses to identify a row
    (an excel_row number for a multi-check language; anything at all for
    a single-pair check, since there's only ever one label there).

    texts: optional {label: translated text} for the SAME labels — when
    given, and there are few enough exceptions (see MAX_EXCEPTIONS_WITH_TEXT
    below), the report includes each exception's actual translated text so
    the manager can see AT A GLANCE what was written differently, instead of
    having to go look up each row number by hand (Александр's own ask,
    2026-09-17). Ignored entirely in single mode (a lone pair has no
    "exceptions" to begin with) or once there are too many to usefully quote.

    Returns None if there's nothing to report at all (no register_value
    entries came back — e.g. "register" wasn't selected, or the AI call
    itself failed and _extract_register_values in app.excel_multi never got
    anything to extract). Otherwise a dict:
      {
        "text": <the plain-text clause this function used to return
                 directly, unprefixed/unpunctuated — still what the Excel
                 export and any other plain-text-only reader uses>,
        "majority": "formal" | "informal" | None,   # None only for the
                     "couldn't determine" case — lets a caller colorize
                     "вы" (formal) and "ты" (informal) differently
                     (Александр asked for blue/orange) without re-parsing
                     the Russian text back out of `text`.
        "exceptions": [{"label": ..., "text": ...}, ...] | None,  # set only
                     when there ARE exceptions AND there are few enough of
                     them AND texts was given — the caller highlights each
                     one's text (Александр asked for red) instead of just
                     a row number.
        "exception_labels": [...] | None,  # set instead of "exceptions"
                     when there are exceptions but either too many of them
                     or no texts were given — same plain "строка N, M, ..."
                     listing as `text` already spells out, just broken out
                     for a caller that wants the raw labels on their own.
      }

    single=True drops the "везде"/"кроме" multi-row framing in favour of a
    plain "на «вы»"/"на «ты»" clause — "everywhere" reads oddly to
    describe a single pair.

    A tie between formal and informal counts (equally split, no real
    majority) resolves to whichever value happened to appear first in
    `values` — deterministic for a given input, but arbitrary as a
    judgment call; a near-even split is exactly the case where the
    manager most needs to look at the actual rows themselves anyway, not
    trust a one-line summary."""
    if not values:
        return None
    classified = {k: v for k, v in values.items() if v in ("formal", "informal")}
    if not classified:
        return {
            "text": "не удалось определить — в переведённых строках нет прямых обращений к пользователю",
            "majority": None,
            "exceptions": None,
            "exception_labels": None,
        }
    if single:
        only_value = next(iter(classified.values()))
        word = "вы" if only_value == "formal" else "ты"
        return {"text": f"на «{word}»", "majority": only_value, "exceptions": None, "exception_labels": None}

    counts: dict[str, int] = {}
    for v in classified.values():
        counts[v] = counts.get(v, 0) + 1
    majority_value = max(counts, key=lambda v: counts[v])
    majority_word = "вы" if majority_value == "formal" else "ты"
    exception_labels = sorted(k for k, v in classified.items() if v != majority_value)
    if not exception_labels:
        return {"text": f"везде на «{majority_word}»", "majority": majority_value, "exceptions": None, "exception_labels": None}

    exceptions_str = ", ".join(str(e) for e in exception_labels)
    row_word = "строка" if len(exception_labels) == 1 else "строки"
    text = f"везде на «{majority_word}», кроме: {row_word} {exceptions_str}"

    # Show the actual (wrongly-toned) text for up to MAX_EXCEPTIONS_WITH_TEXT
    # exceptions, so the manager sees what was written differently without
    # hunting down each row — beyond that, a wall of quoted text is harder
    # to scan than the short numeric list `text` above already gives, so it
    # falls back to just the labels (Александр's own cutoff: "если строк ...
    # более трёх, то тогда уже лучше перечислить их номера").
    exceptions_detail = None
    if texts and len(exception_labels) <= MAX_EXCEPTIONS_WITH_TEXT:
        exceptions_detail = [{"label": lbl, "text": texts.get(lbl, "")} for lbl in exception_labels]

    return {
        "text": text,
        "majority": majority_value,
        "exceptions": exceptions_detail,
        "exception_labels": None if exceptions_detail is not None else exception_labels,
    }


def _allowed_ai_types(checks: list[str]) -> set[str]:
    """The finding "type" values this run is actually allowed to return —
    whatever was requested, restricted to the AI check types that exist at
    all. Used as a hard filter on the model's response: the prompt already
    tells the model to check only these, but a model doesn't always listen
    perfectly (a glaring, unrelated problem can slip through anyway), so
    this guarantees a check the manager didn't ask for never shows up in
    the results, rather than just hoping the prompt was followed.

    REGISTER_VALUE_TYPE is added on top of CHECK_LABELS' own keys (rather
    than living in CHECK_LABELS itself) because it isn't a problem type at
    all — see that dict's comment — so it needs its own opt-in here."""
    allowed = {c for c in checks if c in CHECK_LABELS}
    if "register" in checks:
        allowed.add(REGISTER_VALUE_TYPE)
    return allowed


def _filter_findings_by_checks(findings: list[dict], checks: list[str]) -> list[dict]:
    allowed = _allowed_ai_types(checks)
    return [f for f in findings if f.get("type") in allowed]


# Languages that get the stronger CLAUDE_MODEL_HARD instead of the default
# CLAUDE_MODEL — agreed with Александр after costing out the difference
# (Sonnet 4.5 is 3x Haiku 4.5 per token, both input and output, but only
# these languages' calls use it, so the total impact is modest). Matched
# against the BASE language subtag of whatever target_lang a check actually
# runs with, so "kk-KZ", "kk", or any other region variant of Kazakh all
# get it alike.
HARD_LANGUAGE_BASES = {"kk", "ky", "tg", "uz", "sw", "te", "mr", "az"}


def _model_for_lang(target_lang: str) -> str:
    base = target_lang.strip().lower().split("-")[0]
    return settings.CLAUDE_MODEL_HARD if base in HARD_LANGUAGE_BASES else settings.CLAUDE_MODEL


# USD per single token (not per million) — verified against
# platform.claude.com/docs/en/about-claude/pricing. Keyed by the exact
# model id, since that's what actually gets billed; if CLAUDE_MODEL or
# CLAUDE_MODEL_HARD is ever pointed at a model not listed here, cost just
# can't be computed for those calls (see _usage_cost) rather than guessing
# at a price that may no longer be current — update this table when that
# happens, or when Anthropic's prices change.
MODEL_PRICING_PER_TOKEN = {
    "claude-haiku-4-5-20251001": {"input": 1.00 / 1_000_000, "output": 5.00 / 1_000_000},
    "claude-sonnet-4-5-20250929": {"input": 3.00 / 1_000_000, "output": 15.00 / 1_000_000},
}
# The Message Batches API (used for large multi-checks — see
# excel_multi.BATCH_THRESHOLD_CHARS) is half price on both input and output.
BATCH_PRICE_DISCOUNT = 0.5


def _usage_cost(model: str, usage: dict | None, batch: bool = False) -> float:
    """USD cost of one API call from its token usage. Returns 0.0 (rather
    than raising) for an unpriced model or missing usage, so a pricing-table
    gap degrades to "cost not shown" instead of breaking the check itself."""
    rates = MODEL_PRICING_PER_TOKEN.get(model)
    if not rates or not usage:
        return 0.0
    cost = usage.get("input_tokens", 0) * rates["input"] + usage.get("output_tokens", 0) * rates["output"]
    return cost * BATCH_PRICE_DISCOUNT if batch else cost


# Generous headroom for a batch prompt covering many rows of one language
# at once (see excel_multi.build_batch_plan — one prompt per language, not
# per row) — raising this costs nothing by itself (Anthropic bills actual
# tokens generated, not the max_tokens ceiling), and a low ceiling is
# exactly what caused Александр's "incomplete report": a large language's
# response hit the old 8000-token cap mid-array and every finding after
# the cut point was silently lost. Comfortably under both models' real
# output limits (Haiku 4.5: 64K; Sonnet: even higher).
AI_MAX_TOKENS = 32000


async def _call_claude(prompt: str, model: str | None = None) -> tuple[str | None, dict, str | None]:
    """Returns (response_text, usage, stop_reason) — usage is Anthropic's raw
    {"input_tokens": int, "output_tokens": int, ...} dict (empty when no API
    key is configured), used by callers to compute and surface this check's
    actual API cost. stop_reason is "max_tokens" when the response was cut
    off mid-generation (the response is then incomplete/truncated JSON) —
    callers use this to warn rather than silently show a partial result as
    if it were complete."""
    if not settings.ANTHROPIC_API_KEY:
        return None, {}, None
    resolved_model = model or settings.CLAUDE_MODEL
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": resolved_model,
                "max_tokens": AI_MAX_TOKENS,
                # Deliberately NOT setting temperature. It was briefly set to
                # 0 here (to make a fixed-criteria classification task give
                # the same answer for the same input every time, instead of
                # varying run to run) but Anthropic rejects it outright with
                # a 400 ("temperature is deprecated for this model") on
                # newer models — confirmed live against Sonnet, which broke
                # every real-time check the moment Sonnet became the default
                # model. Anthropic's own guidance for these newer models:
                # "Remove them from requests, and use prompting to guide the
                # model's behavior instead" — there's no replacement
                # determinism knob, so consistency now has to come from
                # clear prompt wording, not a request parameter.
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        data = resp.json()

    text = next((b["text"] for b in data.get("content", []) if b.get("type") == "text"), None)
    return text, data.get("usage", {}), data.get("stop_reason")


def _salvage_json_objects(text: str) -> list:
    """Best-effort recovery when the model's JSON array response got cut off
    mid-array (hit max_tokens) — rather than losing every finding in the
    batch just because the last entry is incomplete, scans for complete
    top-level {...} objects (respecting quoted strings, so a brace inside a
    message string doesn't confuse the count) and parses each on its own,
    keeping whatever came through whole and discarding only the truncated
    tail."""
    objects = []
    depth = 0
    start = None
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                candidate = text[start:i + 1]
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict):
                        objects.append(obj)
                except Exception:
                    pass
                start = None
    return objects


def parse_json_array(text_block: str | None) -> list:
    if not text_block:
        return []
    cleaned = re.sub(r"```json|```", "", text_block).strip()
    try:
        result = json.loads(cleaned)
        if isinstance(result, list):
            return result
    except Exception:
        pass
    return _salvage_json_objects(cleaned)


def _truncation_warning() -> dict:
    """A synthetic finding (not from the model) injected whenever a
    response was cut off by the max_tokens ceiling — makes an otherwise
    silent, partial result visible instead of just looking like a clean
    "no problems found" report."""
    return {
        "type": "system",
        "severity": "high",
        "message": (
            "Ответ ИИ был обрезан из-за большого объёма материала (слишком много текста и/или находок "
            "для одного запроса) — часть строк могла остаться непроверенной нейросетью. Бесплатные "
            "автоматические проверки (числа, теги, пунктуация) при этом всё равно отработали по всем "
            "строкам. Попробуйте проверить этот язык отдельно от остальных или уменьшить число выбранных "
            "критериев за один прогон."
        ),
    }


_AI_FAILURE_REASONS_RU = {
    "errored": "ошибка на стороне ИИ-сервиса",
    "expired": "истекло время ожидания ответа",
    "canceled": "запрос был отменён",
}


def _ai_failure_warning(reason: str) -> dict:
    """A synthetic finding for when the AI request for a language never
    produced any usable result at all (errored/expired/canceled batch
    request, or a result that never came back) — otherwise this looks
    identical to "checked, nothing found"."""
    return {
        "type": "system",
        "severity": "high",
        "message": (
            f"ИИ-проверка для этого языка не выполнилась ({_AI_FAILURE_REASONS_RU.get(reason, reason)}) — "
            "свяжитесь с нами, чтобы разобраться. Бесплатные автоматические проверки всё равно "
            "отработали по всем строкам."
        ),
    }


async def run_ai_checks(
    source: str, translation: str, checks: list[str], extra_instructions: str = "",
    target_lang: str = "", source_lang: str = "",
) -> tuple[list[dict], float]:
    """Returns (findings, cost_usd) — cost_usd is this one API call's actual
    cost from Anthropic's reported token usage (0.0 when no AI check ran,
    e.g. no API key configured or nothing to check against).

    checks_description being empty (nothing to check at all) short-circuits
    before even considering register — but note that "register" alone
    (with no other AI check selected) still needs a real API call: unlike
    the other checks, it has no CHECK_LABELS entry of its own, so
    _checks_description('register' only) legitimately returns None while
    _register_instructions still has something to ask for. Guarded
    against below by checking checks_description OR "register" in checks,
    not just checks_description alone."""
    checks_description = _checks_description(checks)
    register_instructions = _register_instructions(checks, batch=False, target_lang=target_lang)
    if not checks_description and not register_instructions:
        return [], 0.0

    prompt = SINGLE_PROMPT.format(
        target_lang_line=_target_lang_line(target_lang),
        calibration=_calibration(checks),
        source_lang_note=_source_lang_note(source_lang, checks),
        source=source,
        translation=translation,
        extra_instructions=extra_instructions.strip() or "нет",
        checks_description=checks_description or "(нет — только сбор информации о регистре обращения ниже)",
        register_instructions=register_instructions,
        register_array_note=_register_array_note(checks, target_lang=target_lang),
        type_enum="|".join(sorted(_allowed_ai_types(checks))),
    )
    model = _model_for_lang(target_lang)
    text_block, usage, stop_reason = await _call_claude(prompt, model=model)
    findings = _filter_findings_by_checks(parse_json_array(text_block), checks)
    if stop_reason == "max_tokens":
        findings = findings + [_truncation_warning()]

    if "register" in checks:
        register_findings = [f for f in findings if f.get("type") == REGISTER_VALUE_TYPE]
        findings = [f for f in findings if f.get("type") != REGISTER_VALUE_TYPE]
        value = register_findings[0].get("value") if register_findings else None
        if value == "mixed":
            # This one pair's own translation switches tone mid-text — a
            # real problem on its own, unrelated to any "majority tone"
            # question (there's nothing else to compare a single pair
            # against anyway), so it's shown as a plain finding instead of
            # going through build_register_report at all.
            findings.append(_register_mixed_finding())
        else:
            report = build_register_report({0: value} if value else {}, single=True)
            if report is not None:
                findings.append({
                    "type": "register_summary",
                    "severity": "low",
                    "message": f"Тон обращения: {report['text']}.",
                    "register_majority": report["majority"],
                })

    return findings, _usage_cost(model, usage)


def build_batch_prompt(
    items: list[dict],
    checks: list[str],
    extra_instructions: str = "",
    target_lang: str = "",
    source_lang: str = "",
) -> tuple[str | None, dict[int, int]]:
    """
    Builds the prompt for one language's batch of (context, source,
    translation) triples, without calling the API — shared by the
    synchronous path (run_ai_checks_batch, below) and the Message Batches
    path (excel_multi.build_batch_plan), so both send an identical prompt
    for the same input.

    items: list of {"context": str, "source": str, "translation": str}, all
    in the same target language. Items with an empty translation are
    skipped (handled by rule checks as "missing translation" instead).

    Returns (prompt, number_to_index) — prompt is None when there's nothing
    to ask the AI (no AI check types selected, or nothing checkable).
    number_to_index maps the 1-based "row" numbers used inside the prompt
    back to the caller's original item indices — pass it to
    group_batch_findings once you have the model's response.
    """
    checks_description = _checks_description(checks)
    register_instructions = _register_instructions(checks, batch=True, target_lang=target_lang)
    if not checks_description and not register_instructions:
        return None, {}

    checkable = [(i, it) for i, it in enumerate(items) if it["translation"].strip()]
    if not checkable:
        return None, {}

    pairs_block = "\n\n".join(
        f'{n}. Контекст: {it["context"] or "—"}\n'
        f'Источник: """{it["source"]}"""\n'
        f'Перевод: """{it["translation"]}"""'
        for n, (_, it) in enumerate(checkable, start=1)
    )
    prompt = BATCH_PROMPT.format(
        target_lang_line=_target_lang_line(target_lang),
        calibration=_calibration(checks),
        source_lang_note=_source_lang_note(source_lang, checks),
        extra_instructions=extra_instructions.strip() or "нет",
        checks_description=checks_description or "(нет — только сбор информации о регистре обращения ниже)",
        register_instructions=register_instructions,
        register_array_note=_register_array_note(checks, target_lang=target_lang),
        type_enum="|".join(sorted(_allowed_ai_types(checks))),
        pairs_block=pairs_block,
    )
    number_to_index = {n: idx for n, (idx, _) in enumerate(checkable, start=1)}
    return prompt, number_to_index


def group_batch_findings(raw: list, number_to_index: dict[int, int]) -> dict[int, list[dict]]:
    """Maps the model's {"row": n, ...} entries back to the caller's item
    indices via the number_to_index from build_batch_prompt.

    An entry can instead carry {"rows": [n1, n2, ...], ...} — the same
    exact problem repeated identically across several pairs, reported ONCE
    per BATCH_PROMPT's own instructions (Александр's ask, 2026-09-17: the
    same mistranslated term showing up in 5 rows shouldn't be 5 separate,
    near-duplicate findings). That's attached to only the FIRST of those
    rows here, carrying every OTHER one's item index in an internal
    "_also_idx" key — app.excel_multi (which has the real Excel row
    numbers, meaningless here) resolves that into the finding's final
    message via its own _resolve_repeated_findings, and strips the key
    before it ever reaches a response. An unrecognized/empty "rows" list
    (every number failed to resolve) is dropped rather than guessed at."""
    grouped: dict[int, list[dict]] = {}
    for entry in raw:
        rows_nums = entry.get("rows")
        if isinstance(rows_nums, list):
            indices = [number_to_index[n] for n in rows_nums if n in number_to_index]
            if not indices:
                continue
            finding = {k: v for k, v in entry.items() if k not in ("row", "rows")}
            if len(indices) > 1:
                finding["_also_idx"] = indices[1:]
            grouped.setdefault(indices[0], []).append(finding)
            continue
        row_num = entry.get("row")
        idx = number_to_index.get(row_num)
        if idx is None:
            continue
        finding = {k: v for k, v in entry.items() if k != "row"}
        grouped.setdefault(idx, []).append(finding)
    return grouped


async def run_ai_checks_batch(
    items: list[dict],
    checks: list[str],
    extra_instructions: str = "",
    target_lang: str = "",
    source_lang: str = "",
) -> tuple[dict[int, list[dict]], float, bool]:
    """Synchronous path: builds the prompt, calls Claude right away, and
    returns (findings keyed by index into items, this call's cost_usd,
    whether the response was truncated by the max_tokens ceiling — the
    caller adds a visible warning for that rather than presenting a
    partial result as a complete one).

    Findings keyed by index here still include any REGISTER_VALUE_TYPE
    entries mixed in with real findings — app.excel_multi extracts and
    summarizes those itself (it's the one with the excel_row numbers to
    label them with), not this function."""
    prompt, number_to_index = build_batch_prompt(
        items, checks, extra_instructions, target_lang, source_lang
    )
    if prompt is None:
        return {}, 0.0, False
    model = _model_for_lang(target_lang)
    text_block, usage, stop_reason = await _call_claude(prompt, model=model)
    raw = parse_json_array(text_block)
    grouped = group_batch_findings(raw, number_to_index)
    filtered = {idx: _filter_findings_by_checks(fs, checks) for idx, fs in grouped.items()}
    return filtered, _usage_cost(model, usage), stop_reason == "max_tokens"


# --------------------------------------------------- Message Batches API ---
# Used for large multi-checks (see excel_multi.BATCH_THRESHOLD_CHARS): all
# per-language requests for one upload are submitted together as a single
# Anthropic batch job at half the normal per-token price. Results usually
# land within an hour rather than immediately — app.main polls for them.

BATCHES_URL = "https://api.anthropic.com/v1/messages/batches"


def _headers() -> dict:
    return {
        "x-api-key": settings.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }


async def create_message_batch(requests: list[dict]) -> str | None:
    """requests: list of {"custom_id": str, "prompt": str, "model": str
    (optional)}. Submits them all as one Anthropic Message Batch — each
    request can specify its own model (see excel_multi.build_batch_plan,
    which sets the per-language model via _model_for_lang), falling back to
    the default CLAUDE_MODEL when omitted — and returns the batch id, or
    None if there's no API key configured or nothing to submit."""
    if not settings.ANTHROPIC_API_KEY or not requests:
        return None
    batch_requests = [
        {
            "custom_id": r["custom_id"],
            "params": {
                "model": r.get("model") or settings.CLAUDE_MODEL,
                "max_tokens": AI_MAX_TOKENS,
                # Deliberately NOT setting temperature — see _call_claude's
                # real-time path above for why: Anthropic rejects it with a
                # 400 on newer models (confirmed live against Sonnet), and
                # recommends prompting instead of a temperature parameter
                # for consistent output on these models.
                "messages": [{"role": "user", "content": r["prompt"]}],
            },
        }
        for r in requests
    ]
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(BATCHES_URL, headers=_headers(), json={"requests": batch_requests})
        resp.raise_for_status()
        return resp.json()["id"]


async def get_batch_status(batch_id: str) -> dict:
    """Raw batch object from Anthropic — notably processing_status
    ("in_progress" | "ended" | "canceling") and results_url (set once ended)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{BATCHES_URL}/{batch_id}", headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def cancel_message_batch(batch_id: str) -> dict:
    """Asks Anthropic to stop processing whatever's left of this batch — a
    manager cancelling a still-processing check. Anthropic moves the batch
    to processing_status "canceling" and then "ended" once every in-flight
    request has settled; anything that hadn't started yet comes back with
    result type "canceled" and isn't billed for. Raises on failure (e.g. the
    batch already ended on Anthropic's side) — the caller decides whether
    that should block anything on our end."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{BATCHES_URL}/{batch_id}/cancel", headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def get_batch_results(results_url: str) -> dict[str, dict]:
    """Fetches and parses the batch's .jsonl results. Returns
    {custom_id: {"text": str | None, "usage": dict, "stop_reason": str |
    None, "result_type": str | None}}. text/usage/stop_reason are None/{}/
    None for any request that errored, expired, or was canceled — handled
    rather than crashing the whole multi-check over one bad language, but
    result_type is passed through either way so the caller (see
    excel_multi.finalize_batch_results) can tell that case apart from a
    genuine "checked, nothing found" and surface it instead of staying
    silent about it."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.get(results_url, headers=_headers())
        resp.raise_for_status()
        raw_text = resp.text

    out: dict[str, dict] = {}
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # One garbled line (a cut-off download, a proxy hiccup) shouldn't
            # take down the whole batch's results — skip just that line and
            # keep parsing the rest; the missing custom_id(s) end up absent
            # from `out`, which finalize_batch_results already treats as
            # "no result for this language" rather than crashing on it.
            continue
        custom_id = entry.get("custom_id")
        if custom_id is None:
            continue
        result = entry.get("result", {})
        result_type = result.get("type")
        text = None
        usage = {}
        stop_reason = None
        if result_type == "succeeded":
            message = result.get("message", {})
            text = next((b["text"] for b in message.get("content", []) if b.get("type") == "text"), None)
            usage = message.get("usage", {})
            stop_reason = message.get("stop_reason")
        out[custom_id] = {"text": text, "usage": usage, "stop_reason": stop_reason, "result_type": result_type}
    return out
