import asyncio
import json
import re

import httpx

from app.config import settings
from app.rule_checks import RULE_BASED_TYPES

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
        "опечатки/ошибки — сюда входят ТРИ разные вещи: (1) обычные опечатки и орфографические ошибки в самом "
        "переводе — неправильно написанное слово, даже если смысл всё равно понятен из контекста (например "
        "«resulits» вместо «results»); (2) ЛЮБАЯ объективная грамматическая ошибка целевого языка — неправильный "
        "падеж, управление, согласование, число, род, форма слова, предлог/послелог, синтаксис и т.п. Для этого "
        "пункта НЕ требуется, чтобы ошибка «ломала» понимание — если форма объективно неправильная по грамматике "
        "целевого языка, это находка, даже когда смысл всё равно можно понять. Важное уточнение про синтаксис "
        "(реальный случай, французский язык, 2026-09-26): если сегмент выглядит синтаксически незавершённым "
        "ТОЛЬКО из-за того, что начинается с союза/связки («и», «а», «но», «et», «mais», «and», «but» и т.п.) или "
        "представляет собой зависимую конструкцию/инфинитив без собственного подлежащего и сказуемого — это, как "
        "правило, НЕ ошибка, а естественное продолжение предыдущего сегмента: в локализуемых текстах одно "
        "предложение по смыслу нередко разбито на несколько соседних ячеек, а текста соседних сегментов у тебя нет, "
        "чтобы это подтвердить. Не сообщай о такой «незавершённости» самой по себе — сообщай только если ВНУТРИ "
        "самого сегмента есть настоящая грамматическая ошибка (неверная форма слова, согласование, падеж и т.п.), "
        "никак не связанная с отсутствием своего подлежащего/сказуемого; (3) ошибки смысла — перевод "
        "означает не то, что исходник: пропущенное отрицание, спутанные число/род, неверно переданный термин, "
        "неверно переданное условие/количество/отношение между частями фразы, потерянный или добавленный смысл. "
        "Сюда же относится ДРУГАЯ ВАЛЮТА, чем в исходнике (например, евро вместо доллара, или другой ISO-код) — "
        "это меняет смысл суммы, а не просто стиль. Сюда же ВСЕГДА относится потеря ограничительного слова при "
        "сумме, сроке, количестве или диапазоне — «от», «до», «не менее», «максимум», «минимум», «только», "
        "«каждый», «не более» и т.п. Это находка ДАЖЕ в коротком заголовке или короткой фразе, где пропажа "
        "одного такого слова может показаться обычным сжатием текста ради краткости — не прощай это как стиль: "
        "такое слово меняет фактическое условие (например, «пополнение от X» — это минимум, а «пополнение на X» "
        "— это точная сумма; «до Y дней» — это верхняя граница, а без «до» это выглядит как ровно Y дней), а не "
        "только формулировку, и читатель перевода поймёт условие иначе, чем в исходнике — именно такие потери "
        "приводят к спорам с пользователями по условиям акций. Не путай это с обычным опущением связки/вводного "
        "слова, не несущего измеримого ограничения (артикль, частица) — здесь теряется конкретное измеримое "
        "ограничение (минимум/максимум/диапазон/исключительность), а не просто слово. Не считай находкой "
        "стилистические предпочтения (другой "
        "синоним с тем же смыслом, другой порядок слов, другая формулировка) — но ТОЛЬКО когда одновременно "
        "выполнены ОБА условия: перевод грамматически корректен И полностью сохраняет смысл исходника. "
        "Синонимичная замена не обязана быть точным словарным соответствием слово-в-слово — если по контексту "
        "она передаёт тот же общий смысл и сообщение для читателя не меняется, это тоже считается сохранением "
        "смысла, а не находкой (например, в рекламном тексте «Опыт вас ждёт уникальный» и «Get ready for an "
        "unforgettable weekend» — «уникальный» и «unforgettable» здесь передают одно и то же приглашение, "
        "и это не искажение смысла, а обычная переводческая адаптация). Если нарушено хотя бы одно из этих "
        "двух условий — это уже не стиль, а настоящая находка по одному из пунктов выше, и о ней нужно "
        "сообщить"
    ),
    "untranslatable": (
        "непереводимые термины — имена турниров/событий/игр/брендов/продуктов/акций, устоявшиеся "
        "маркетинговые слова, которые обычно оставляют как есть (например «VIP», «Lootbox», название турнира вроде "
        "«Grand Prix»), А ТАКЖЕ устоявшиеся сокращения/аббревиатуры проекта на английском, которые в исходнике "
        "последовательно используются НЕ расшифрованными (например «FS» вместо «free spins» — если в самом "
        "исходнике рядом также встречается расшифрованный вариант «free spins», это не противоречие: значит, "
        "источник сам иногда сокращает, а иногда пишет полностью, и перевод должен зеркалить именно то, что стоит "
        "в конкретной паре — сокращение остаётся сокращением, а расшифровка переводится как обычный текст). Сильный "
        "сигнал, что термин непереводимый: если он уже в САМОМ ИСХОДНИКЕ оставлен нетронутым "
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
        "есть, хотя должны были быть переведены (это НЕ относится к отдельным именам/брендам/терминам/устоявшимся "
        "сокращениям вроде «FS», которые правильно оставлены нетронутыми намеренно — за них отвечает отдельная "
        "проверка «непереводимые термины», и там это не находка); (2) весь перевод "
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
        "переведён правильно; если стрелка в переводе осталась ровно как в исходнике — находки быть не должно; "
        "(5) порядковое числительное, обозначающее место/позицию/ранг, или диапазон таких мест (например «1st "
        "place», «4th-15th place», «Top 10th») — если в переводе само число сохранено, а грамматическое "
        "окончание/суффикс порядкового числительного при нём (например английские «-st»/«-nd»/«-rd»/«-th», или "
        "соответствующее окончание другого языка) ОПУЩЕНО — это ДОПУСТИМО и НЕ находка, даже если это окончание "
        "есть в русском или английском исходнике: опускать его при переводе разрешено. Это исключение — именно "
        "и только про окончание порядкового числительного у места/ранга; любая другая неполнота (само число "
        "пропало, пропало слово «место»/«place» целиком там, где это меняет смысл фразы, и т.п.) по-прежнему "
        "остаётся находкой по общим правилам выше"
    ),
}

# CALIBRATION_STRICT_OPENING (the confidence-bar sentence) and
# _CALIBRATION_SHARED_TAIL (everything else — currency/date formatting
# notes, multi-finding-per-pair rules) are kept as separate constants only
# so smoketest can check the confidence wording actually landed in a
# prompt without re-parsing the whole calibration text.
#
# There used to be a second, "relaxed" opening here too (a lowered-
# confidence test mode, wired into a "🔬 Тест калибровки" checkbox on the
# upload form) — added 2026-09-22 to find out whether real-world misses on
# hard-language findings came from the model's own knowledge gap or from
# this confidence bar filtering out a correct-but-uncertain finding.
# Removed 2026-09-23 after the real Marathi test settled the question: the
# relaxed opening made ZERO difference to Sonnet's result (still missed the
# same error), so the confidence bar alone was never the cause — the actual
# fix at the time was BATCH_PROMPT_SINGLE_ITEM below. See the translation-QA
# catalog doc (section 2) for the full writeup Александр reviewed before
# that removal.
#
# Reworded again 2026-09-23, later the same day, once the Kyrgyz/French
# investigation (model-comparison diagnostic, see app.model_comparison)
# found the real mechanism: the OLD wording here ("сообщай, если уверен(а),
# что это ошибка, а НЕ другой допустимый вариант") told the model to lean
# toward silence on doubt, and CHECK_LABELS["typo"]'s old "грамматика,
# ломающая понимание" phrase went further and told it outright that an
# understandable-but-wrong grammatical form doesn't even qualify as a
# finding to be uncertain ABOUT in the first place — confirmed live: the
# real "{{amount}} баштап" case-ending miss returned a genuinely EMPTY raw
# response (not a filtered-out one — see model_comparison.raw_responses)
# from both Sonnet and Haiku under the old wording, every single run, while
# a bare, unstructured version of the same question caught it. This opening
# now says what TO report (every objective finding) rather than gating on
# confidence about what NOT to report — the actual "don't flag pure style"
# guardrail moved into CHECK_LABELS["typo"]'s own two-condition test
# instead (grammatically correct AND fully meaning-preserving), which is
# harder to satisfy by accident than the old one-line "tell them apart"
# instruction was.
CALIBRATION_STRICT_OPENING = (
    "Общее правило: сообщай обо всех объективных находках по каждому выбранному критерию — не обязательно быть "
    "стопроцентно уверенным(ой), чтобы сообщить о реальной проблеме. Не сообщай только о том, что является другим, "
    "тоже полностью допустимым и корректным вариантом перевода — критерии ниже сами объясняют, когда именно "
    "находка считается таким допустимым вариантом, а когда нет."
)
_CALIBRATION_SHARED_TAIL = (
    "Порядок символа валюты относительно числа, "
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
# Kept as a public name (imported/used elsewhere, e.g. tests) meaning "the
# strict/production calibration text in full" — equivalent to what this
# used to be as one single constant before the split above.
CALIBRATION_BASE = f"{CALIBRATION_STRICT_OPENING} {_CALIBRATION_SHARED_TAIL}"

# Александр's ask, 2026-09-18 (part of moving to Opus everywhere on the
# hard-language list, and wanting to afford it): the model's OWN written
# answer costs several times more per token than what we send it, so a
# shorter "message" directly cuts the bill — purely a writing-style
# instruction, doesn't change what counts as a real finding or how
# carefully it's judged. Asks for a compact "суть — короткая цитата
# исходник/перевод" shape instead of a full explanation of why it matters.
_CONCISENESS_INSTRUCTION = (
    "Пиши поле \"message\" МАКСИМАЛЬНО КОРОТКО, но так, чтобы было понятно, к какому месту в тексте это "
    "относится и в чём разница — называй суть проблемы в двух-трёх словах, затем сразу короткую цитату "
    "исходника и перевода в кавычках, без развёрнутых объяснений, почему это ошибка или как её поймёт "
    "пользователь. Например, вместо длинного варианта: \"Искажён смысл: «Secure position» — это призыв к "
    "действию («закрепите/обеспечьте своё место в рейтинге»), а перевод «안정적인 위치» означает "
    "«стабильное/надёжное местоположение», то есть описание, а не действие пользователя.\" — пиши коротко: "
    "\"Искажение: «Secure position» — «закрепите место», а «안정적인 위치» — «стабильное положение».\" "
    "Сокращай только форму, а не суть — конкретная фраза и разница должны остаться понятны."
)

# Александр's ask, 2026-09-26: comments were sometimes coming back ENTIRELY
# in the target language — fine for the model reasoning internally, but
# useless to him, since he reads Russian, not (in his real examples)
# Turkish. Real verbatim examples he hit: "Bitişik yazım: 'haftasonunun' —
# doğrusu ayrı 'hafta sonunun'.", "Hatalı/doğal olmayan yapı: '...' — düşük
# çekim/sözdizimi hatalı.", "İsim hatası: kaynakta 'Кельвин Харрис' ...
# çeviride 'Kelvin Harris' olarak yazılmış, doğrusu 'Calvin Harris'." — not
# one word of that is Russian. Rather than ban quoting the target-language
# text (the exact quote is often the most useful, precise part — see
# _CONCISENESS_INSTRUCTION above), require the surrounding explanation —
# what the problem IS, in general terms — to always be in Russian, with a
# short Russian gloss for any target-language word/phrase that isn't
# self-evident from the quote alone (a spelling fix like "hafta sonunun"
# needs no gloss; a named-entity mix-up like «Kelvin Harris» → «Calvin
# Harris» does, since a non-Turkish-speaking reader can't otherwise tell
# which one is right).
_RUSSIAN_COMMENT_INSTRUCTION = (
    "ВАЖНО: поле \"message\" целиком читает русскоговорящий человек, который не обязательно знает целевой "
    "язык перевода. Само объяснение — в чём проблема — всегда пиши по-русски, даже если целевой язык совсем "
    "не похож на русский (например турецкий, корейский, арабский). Можно и нужно приводить точную цитату на "
    "целевом языке в кавычках (это самая полезная, точная часть), но она должна идти ВНУТРИ русского "
    "предложения, а не заменять его — недопустимо, чтобы всё поле \"message\" было написано на целевом языке "
    "без русского объяснения вообще. Если цитата на целевом языке сама по себе непонятна русскоговорящему "
    "без перевода (например путаница имён/названий, например «Kelvin Harris» вместо «Calvin Harris», или "
    "слово, значение которого не очевидно из контекста) — добавь короткий перевод или пояснение на русском "
    "прямо рядом с цитатой в скобках. Пример неправильного (слишком) формата: \"Bitişik yazım: 'haftasonunun' "
    "— doğrusu ayrı 'hafta sonunun'.\" — целиком на турецком, непонятно без словаря. Пример правильного "
    "формата на том же материале: \"Слитное написание: «haftasonunun» вместо раздельного «hafta sonunun» "
    "(«выходных»).\""
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
    return (
        f"{CALIBRATION_STRICT_OPENING} {_CALIBRATION_SHARED_TAIL} {tail} {_CONCISENESS_INSTRUCTION} "
        f"{_RUSSIAN_COMMENT_INSTRUCTION}"
    )


# Split into PREFIX/SUFFIX for the same reason/see the same comment as
# FINDINGS_SEARCH_PROMPT's own split above — target_lang_line/calibration/
# source_lang_note are all fixed per (language, checks) pair, so this
# prefix repeats byte-for-byte across every call this specific manual
# single-pair check makes for the same language. extra_instructions can't
# safely join the cacheable prefix here without moving it earlier than the
# source/translation text — SINGLE_PROMPT's own field order already has it
# AFTER the row content, and reordering existing prompt wording is exactly
# what this split deliberately avoids (see _call_claude's cache_prefix
# comment). SINGLE_PROMPT itself is still the same exact text as before.
_SINGLE_PROMPT_PREFIX = """Ты — модуль контроля качества перевода для бюро переводов. Даны исходный текст и перевод.
Проверяй только критерии из "Что проверять" ниже.

{target_lang_line}

{calibration}

{source_lang_note}

"""
_SINGLE_PROMPT_SUFFIX = """Исходный текст:
\"\"\"{source}\"\"\"

Перевод:
\"\"\"{translation}\"\"\"
{prior_findings}
Особые указания к задаче (важнее общих правил, если есть):
{extra_instructions}

Что проверять: {checks_description}
{other_type_instruction}
{register_instructions}
Верни ТОЛЬКО валидный JSON-массив без markdown и пояснений, строго в этой форме
(пустой массив [], если проблем нет{register_array_note}):
[
  {{"type": "{type_enum}", "severity": "low|medium|high", "message": "конкретное описание на русском, с указанием места в тексте, если уместно"}}
]"""
SINGLE_PROMPT = _SINGLE_PROMPT_PREFIX + _SINGLE_PROMPT_SUFFIX

# Split into PREFIX/SUFFIX — same reasoning as FINDINGS_SEARCH_PROMPT's own
# split above, but the biggest win of the five: everything in PREFIX below
# (target_lang_line, calibration, source_lang_note, extra_instructions,
# checks_description, other_type_instruction, AND the two long static
# paragraphs about cross-row duplicate reporting and never citing pair
# numbers) is fixed for the WHOLE document/run, not just one language —
# build_batch_prompt already computes every one of these before it even
# knows which rows are in THIS particular chunk. Only {pairs_block} (the
# actual rows) and {register_instructions} are truly per-chunk. That means
# this prefix repeats byte-for-byte across every chunk of every language in
# one multi-check — including, especially, the hard-language path
# (MAX_ROWS_PER_AI_CALL_HARD = 1 row/call), where it would otherwise never
# get to amortize across 15 rows the way a normal language's batches do.
# BATCH_PROMPT itself is still the exact same text as before — no wording
# or order changed, see _call_claude's own cache_prefix comment for why
# that's what makes this safe to ship without a side-by-side accuracy test.
_BATCH_PROMPT_PREFIX = """Ты — модуль контроля качества перевода для бюро переводов. Даны пары (контекст, исходный текст, перевод) на один целевой язык.
Проверяй только критерии из "Что проверять" ниже. По умолчанию оценивай каждую пару отдельно от остальных — но если
описание конкретного критерия ниже прямо просит сравнить пары между собой, следуй этому описанию для этого критерия.

{target_lang_line}

{calibration}

{source_lang_note}

Особые указания к задаче (важнее общих правил, если есть):
{extra_instructions}

Что проверять: {checks_description}
{other_type_instruction}

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

"""
_BATCH_PROMPT_SUFFIX = """Пары для проверки:
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
BATCH_PROMPT = _BATCH_PROMPT_PREFIX + _BATCH_PROMPT_SUFFIX

# A leaner variant of BATCH_PROMPT for the case where a "batch" happens to
# hold exactly ONE checkable pair — Александр's real test, 2026-09-22: the
# SAME Marathi pair, checked as a 1-ROW document upload (so batching many
# rows together isn't even in play here — MAX_ROWS_PER_AI_CALL_HARD already
# makes every hard-language row its own call), was still missed through the
# document path, while the SAME exact text through the single-pair fields
# form (SINGLE_PROMPT below) caught it reliably. The difference isn't row
# COUNT — it's that build_batch_prompt always used the full BATCH_PROMPT
# text above, which spends a large block explaining cross-row duplicate
# detection ("Повторяется по всему документу", "rows" vs "row", comparing
# pairs against each other) — instructions that are simply meaningless with
# only one pair to look at, but were still being sent and, it turns out,
# apparently distracting enough to cost real accuracy on subtle findings.
# This keeps the SAME "row"-numbered JSON response shape as BATCH_PROMPT
# (so group_batch_findings/number_to_index need no special-casing) while
# dropping every instruction that only makes sense with 2+ pairs to compare —
# functionally converging on SINGLE_PROMPT's simplicity without a second,
# differently-shaped response format to parse.
# Split into PREFIX/SUFFIX — same reasoning as BATCH_PROMPT's own split
# above. This is the template the hard-language path actually uses one row
# at a time (see this constant's own comment above), so caching its prefix
# matters most here: without it, this exact block of instructions would be
# billed at full price on EVERY single row for hi/hing/mr/te/etc., with no
# 15-row batch to spread it across the way normal languages get. Unlike
# BATCH_PROMPT, checks_description/other_type_instruction sit AFTER the
# per-row context/source/translation in this template's own field order —
# so, same rule as SINGLE_PROMPT's own split, they stay out of the
# cacheable prefix rather than reordering existing wording. extra_instructions
# CAN join the prefix here, unlike in SINGLE_PROMPT, because this template
# already places it before the row content. BATCH_PROMPT_SINGLE_ITEM itself
# is still the exact same text as before.
_BATCH_PROMPT_SINGLE_ITEM_PREFIX = """Ты — модуль контроля качества перевода для бюро переводов. Дана одна пара (контекст, исходный текст, перевод).
Проверяй только критерии из "Что проверять" ниже.

{target_lang_line}

{calibration}

{source_lang_note}

Особые указания к задаче (важнее общих правил, если есть):
{extra_instructions}

"""
_BATCH_PROMPT_SINGLE_ITEM_SUFFIX = """Контекст: {context}
Исходный текст:
\"\"\"{source}\"\"\"

Перевод:
\"\"\"{translation}\"\"\"
{prior_findings}
Что проверять: {checks_description}
{other_type_instruction}

Важно про сам текст "message": НИКОГДА не упоминай в нём номер пары/строки — ни словом ("пара 1", "строка 1"), ни
просто числом в скобках. Если нужно различить конкретные места (например, при нескольких предложениях в одном
тексте) — используй ТОЛЬКО цитаты самого текста (конкретную фразу или предложение), а не номер.
{register_instructions}
Верни ТОЛЬКО валидный JSON-массив без markdown и пояснений, строго в этой форме
(пустой массив [], если проблем нет{register_array_note}):
[
  {{"row": 1, "type": "{type_enum}", "severity": "low|medium|high", "message": "конкретное описание на русском, с указанием места в тексте, если уместно"}}
]"""
BATCH_PROMPT_SINGLE_ITEM = _BATCH_PROMPT_SINGLE_ITEM_PREFIX + _BATCH_PROMPT_SINGLE_ITEM_SUFFIX


# ------------------------------------------------------- two-step pipeline ---
# Step 1 ("search"): FINDINGS_SEARCH_PROMPT below, an intentionally
# UNCONSTRAINED first pass — no CHECK_LABELS category schema, no
# calibration/confidence-bar wording at all. Александр's ask, 2026-09-23,
# after the Kyrgyz/French investigation (see CALIBRATION_STRICT_OPENING's
# own comment for the full backstory): all session, an unstructured/bare
# prompt kept catching real errors that the structured, calibrated prompt
# missed — not because the model lacked the knowledge, but because the
# structured prompt's own type list and calibration wording quietly steer
# it toward staying quiet about anything that doesn't cleanly fit a named
# category or clear a confidence bar. Patching that prompt's wording
# error-type by error-type doesn't scale (Александр's own words) — so
# instead of teaching the structured prompt every individual shape of
# mistake by hand, this runs a first, completely open pass to surface
# candidates, then hands them to the EXISTING, already-tuned structured
# prompt as extra per-pair context (see _prior_findings_block) for Step 2.
# All the real filtering/categorizing logic (CHECK_LABELS, calibration,
# _filter_findings_by_checks, the type enum) stays exactly as it already
# is and does double duty: it drops whatever Step 1 got wrong (style,
# false leads) AND still independently catches whatever Step 1 missed,
# exactly as it always could on its own. This is also why Opus was retired
# the same day (see HARD_LANGUAGE_BASES's own comment) — two Sonnet calls
# turned out cheap enough, and together effective enough, to replace what
# one Opus call alone was covering.
#
# Reuses the same numbered "N. Контекст/Источник/Перевод" pairs_block shape
# BATCH_PROMPT uses (via _pairs_block) so ONE code path (_search_findings)
# covers both run_ai_checks's single pair and run_ai_checks_batch's many —
# no separate "single item" variant is needed here the way
# BATCH_PROMPT_SINGLE_ITEM exists for the structured prompt, since there's
# no cross-row-duplicate machinery to strip out of a free-text search pass
# in the first place.
# Split into PREFIX/SUFFIX (2026-09-25, Александр's cost-cutting ask) so
# _search_findings/_search_findings_openai can hand _call_claude the exact
# leading substring that's byte-identical across every chunk of the SAME
# language (target_lang_line and source_lang_note are both fixed for a
# whole run) — Anthropic's prompt caching then bills that repeated prefix
# at ~10% of its normal price on every call after the first, instead of
# full price every time. FINDINGS_SEARCH_PROMPT itself (below) is still
# the exact same byte-for-byte text as before — just prefix+suffix glued
# back together — so every existing caller/test that reads
# FINDINGS_SEARCH_PROMPT directly (model_comparison.py, smoketest.py) is
# unaffected. No wording or ordering changed — see _call_claude's own
# cache_prefix comment for why this is safe.
_FINDINGS_SEARCH_PROMPT_PREFIX = """Ты — опытный редактор переводов. Даны пары (контекст, исходный текст, перевод).
Прочитай их совершенно свободно, БЕЗ заранее заданного списка типов ошибок и БЕЗ формальной шкалы уверенности —
просто внимательно сверь каждую пару и отметь всё, что кажется тебе неправильным, сомнительным, нелогичным или
просто заслуживающим внимания редактора: опечатки, грамматика, искажение смысла, пропуски, странности стиля —
что угодно, вплоть до мелочей. Отметить лишнее не страшно (это перепроверят и при необходимости отсеют на
следующем шаге) — а вот промолчать о том, что реально не так, нежелательно.

{target_lang_line}

{source_lang_note}

"""
_FINDINGS_SEARCH_PROMPT_SUFFIX = """Пары для проверки:
{pairs_block}

Для каждой пары, где ты что-то заметил, напиши отдельную строку в формате:
NUMBER: короткое, но конкретное описание проблемы
где NUMBER — номер пары из списка выше (можно несколько строк на одну и ту же пару, если проблем в ней несколько).
Пары, где всё в порядке, просто пропусти — не пиши по ним ничего. Если проблем нет вообще нигде — верни ровно одну
строку: "проблем не найдено". Не используй JSON, markdown, вступления или заключения — только такие строки, по
одной на строку."""
FINDINGS_SEARCH_PROMPT = _FINDINGS_SEARCH_PROMPT_PREFIX + _FINDINGS_SEARCH_PROMPT_SUFFIX


def _checkable_items(items: list[dict]) -> list[tuple[int, dict]]:
    """Items with a non-empty translation, paired with their ORIGINAL index
    into `items` — shared by build_batch_prompt and _search_findings so
    both number pairs identically, which matters because prior_findings
    (Step 1's output, keyed by this same original index) has to line up
    with build_batch_prompt's own numbering when Step 2's prompt is built."""
    return [(i, it) for i, it in enumerate(items) if it["translation"].strip()]


def _prior_findings_block(candidates: list[str] | None) -> str:
    """Turns Step 1's raw, unconstrained candidate list for ONE pair into
    the block embedded in Step 2's (already-tuned, structured) prompt right
    next to that same pair — empty string when Step 1 found nothing there,
    so a pair with no candidates reads exactly as it did before the
    two-step pipeline existed. Deliberately tells the model these are
    unverified leads, not confirmed findings — Step 2's own criteria still
    decide what actually gets reported; this only makes sure nothing Step 1
    noticed gets silently lost before Step 2 even sees it."""
    if not candidates:
        return ""
    lines = "\n".join(f"- {c}" for c in candidates)
    return (
        "\nЧерновой, ничем не ограниченный просмотр уже заметил в этой паре следующее (это не готовые находки, "
        "а просто наводки — оцени каждую по критериям выше и ниже, отбрось то, что при внимательной проверке "
        "окажется просто стилем или не относится ни к одному критерию, и по-прежнему сам ищи всё, что этот "
        f"черновой просмотр мог пропустить):\n{lines}\n"
    )


def _pairs_block(checkable: list[tuple[int, dict]], prior_findings: dict[int, list[str]] | None = None) -> str:
    """Numbered 'N. Контекст/Источник/Перевод' block shared by BATCH_PROMPT
    and FINDINGS_SEARCH_PROMPT — checkable is [(original_item_index, item),
    ...] (see _checkable_items); N is assigned by POSITION here (1, 2, 3,
    ...), not by original_item_index. prior_findings, when given, is keyed
    by that original_item_index (the same key space run_ai_checks_batch and
    _search_findings both use for their own return values) — each pair
    gets its own Step 1 candidates embedded right after it, via
    _prior_findings_block."""
    parts = []
    for n, (idx, it) in enumerate(checkable, start=1):
        block = (
            f'{n}. Контекст: {it["context"] or "—"}\n'
            f'Источник: """{it["source"]}"""\n'
            f'Перевод: """{it["translation"]}"""'
        )
        if prior_findings:
            block += _prior_findings_block(prior_findings.get(idx))
        parts.append(block)
    return "\n\n".join(parts)


_SEARCH_LINE_RE = re.compile(r"^(\d+)\s*[:.]\s*(.+)$")


def _parse_search_findings(text_block: str | None, checkable: list[tuple[int, dict]]) -> dict[int, list[str]]:
    """Parses FINDINGS_SEARCH_PROMPT's plain "NUMBER: description" lines
    back into {original_item_index: [description, ...]}, using the same
    checkable list (see _checkable_items) _search_findings numbered the
    pairs with — mirrors what group_batch_findings does for the structured
    prompt's "row" field, just for free text instead of JSON. A line that
    doesn't parse (wrong shape, an out-of-range number, the "проблем не
    найдено" sentinel, stray commentary) is silently skipped rather than
    raising — this is free text, not JSON, so it's expected to be looser
    than the structured response ever is."""
    if not text_block:
        return {}
    number_to_index = {n: idx for n, (idx, _) in enumerate(checkable, start=1)}
    found: dict[int, list[str]] = {}
    for line in text_block.splitlines():
        line = line.strip().lstrip("-•* ").strip()
        if not line:
            continue
        m = _SEARCH_LINE_RE.match(line)
        if not m:
            continue
        idx = number_to_index.get(int(m.group(1)))
        if idx is None:
            continue
        desc = m.group(2).strip()
        if desc:
            found.setdefault(idx, []).append(desc)
    return found


async def _search_findings(
    items: list[dict], target_lang: str = "", source_lang: str = "", model_override: str | None = None,
) -> tuple[dict[int, list[str]], float]:
    """Step 1 of the two-step pipeline — see FINDINGS_SEARCH_PROMPT's own
    comment above for the full rationale. Returns ({}, 0.0) with NO API
    call at all when there's nothing checkable (mirrors build_batch_prompt's
    own early-outs) — no point spending a whole extra call to find nothing.

    Resolves its model via model_override or _model_for_lang(target_lang) —
    same precedence run_ai_checks_batch itself uses for Step 2 — so a
    caller that forces a specific model (currently only
    app.model_comparison's diagnostic, indirectly, and
    run_ai_checks_batch's own model_override passthrough) gets that same
    model for BOTH steps, not Step 1 silently running under whatever
    _model_for_lang would have picked instead."""
    checkable = _checkable_items(items)
    if not checkable:
        return {}, 0.0
    # cache_prefix: see _FINDINGS_SEARCH_PROMPT_PREFIX's own comment — fixed
    # per (target_lang, source_lang), so it repeats across every chunk of
    # the same language's Step 1 calls within one run.
    cache_prefix = _FINDINGS_SEARCH_PROMPT_PREFIX.format(
        target_lang_line=_target_lang_line(target_lang),
        source_lang_note=_source_lang_note(source_lang),
    )
    prompt = cache_prefix + _FINDINGS_SEARCH_PROMPT_SUFFIX.format(pairs_block=_pairs_block(checkable))
    model = model_override or _model_for_lang(target_lang)
    text_block, usage, _stop_reason = await _call_claude(prompt, model=model, cache_prefix=cache_prefix)
    return _parse_search_findings(text_block, checkable), _usage_cost(model, usage)


async def _search_findings_openai(
    items: list[dict], target_lang: str = "", source_lang: str = "",
) -> tuple[dict[int, list[str]], float]:
    """Step 1 of the two-step pipeline, run under OpenAI's model instead of
    Claude — reuses the exact same FINDINGS_SEARCH_PROMPT and the exact
    same free-text "NUMBER: description" parsing (_parse_search_findings)
    as _search_findings itself; only the API call underneath differs (see
    _call_openai). Returns ({}, 0.0) with no call at all when there's
    nothing checkable OR no OPENAI_API_KEY is configured (see
    _call_openai's own missing-key behavior) — a caller can treat this
    exactly like "this branch contributed nothing this time" either way."""
    checkable = _checkable_items(items)
    if not checkable:
        return {}, 0.0
    prompt = FINDINGS_SEARCH_PROMPT.format(
        target_lang_line=_target_lang_line(target_lang),
        source_lang_note=_source_lang_note(source_lang),
        pairs_block=_pairs_block(checkable),
    )
    text_block, usage, _stop_reason = await _call_openai(prompt)
    return _parse_search_findings(text_block, checkable), _openai_usage_cost(settings.OPENAI_MODEL, usage)


_BRANCH_FAILURE_EXCEPTIONS = (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError)
# Catches more than just httpx.HTTPError (a non-2xx response or a
# lower-level connection failure) — an independent review of the Step 1
# ensemble (2026-09-23) pointed out that a vendor (or a proxy in between)
# can also return a 200 with a garbled or unexpected-shaped body:
# resp.json() then raises json.JSONDecodeError (a ValueError, not an
# httpx.HTTPError), and _call_openai's own response parsing
# (choices[0]/message/content) can raise KeyError/TypeError/AttributeError
# if that shape isn't what's expected. Any of these is exactly the same
# kind of "this one branch had a bad moment" failure as an HTTP error.


async def _run_search_branch(coro) -> tuple[tuple[dict[int, list[str]], float], Exception | None]:
    """Runs one Step 1 branch of the ensemble below and turns a failure
    from THAT branch alone into an empty, zero-cost contribution — rather
    than letting it sink the other branch's results too (same resilience
    fix the earlier, since-reverted Sonnet+Haiku ensemble needed) — while
    still handing the exception back to the caller, so _ensemble_search_
    findings can tell "this branch wasn't even configured" apart from
    "this branch was configured but broke" and only warn about the
    latter (see _ensemble_search_findings's own comment)."""
    try:
        return await coro, None
    except _BRANCH_FAILURE_EXCEPTIONS as exc:
        return ({}, 0.0), exc


def _model_branch_search_warning(model_label: str) -> dict:
    """A visible "type": "system" finding (same synthetic-finding pattern
    as _truncation_warning/_ai_failure_warning elsewhere in this file) for
    when a model that WAS configured to run on Step 1's ensemble search
    (see _ensemble_search_findings) actually failed to contribute —
    Александр's explicit ask (2026-09-23): he wants to be able to tell,
    from the report itself, that both models really did run, rather than
    the ensemble silently and permanently degrading to one model (e.g. an
    OpenAI account running out of credit) with no visible trace at all."""
    return {
        "type": "system",
        "severity": "medium",
        "message": (
            f"Поиск ошибок на первом шаге не сработал для модели {model_label} (сбой на её стороне — "
            "например, закончились доступные средства на счёте, неверный/просроченный ключ API, или "
            "временная недоступность сервиса). Проверка всё равно выполнена полностью — второй, "
            "проверяющий шаг по-прежнему сработал — но БЕЗ вклада этой модели в поиск. Если это "
            f"повторяется часто, стоит проверить баланс/ключ API для {model_label}."
        ),
    }


async def _ensemble_search_findings(
    items: list[dict], target_lang: str = "", source_lang: str = "", model_override: str | None = None,
) -> tuple[dict[int, list[str]], float, list[dict]]:
    """Step 1 of the two-step pipeline, Александр's ask (2026-09-23): run
    it under Sonnet (Anthropic) and GPT (OpenAI) concurrently and merge
    their candidates, instead of Sonnet alone. The two calls run via
    asyncio.gather, hitting two entirely separate vendors at once — unlike
    the earlier Sonnet+Haiku ensemble, this does NOT double Anthropic's own
    concurrent-call load (see excel_multi.AI_CONCURRENCY), since only one
    of the two calls here is ever an Anthropic call.

    Motivation this time is different from that earlier, reverted
    ensemble: Kyrgyz detection was still inconsistent even on the byte-
    identical, already-proven plain Sonnet+Sonnet pipeline, most likely
    ordinary run-to-run LLM variance rather than a code regression — no
    concrete failing example was available to test against this time.
    Haiku shares Sonnet's own training lineage and, per that earlier
    experiment, likely shared its blind spots too; a model from a
    genuinely different vendor is the more principled bet on catching
    whatever Sonnet alone might occasionally miss on a given run — though,
    same caveat as before, this is unproven and mainly trades cost for a
    SECOND independent pass, not a guaranteed fix.

    Falls back to plain _search_findings (no GPT branch at all, empty
    warnings list) when model_override is given — mirrors run_ai_checks_
    batch's own model_override passthrough from before: a caller forcing a
    specific model (currently only app.model_comparison's diagnostic) gets
    exactly that model for Step 1, not a silent extra GPT call it never
    asked for.

    Returns a third element, `warnings` — a list of synthetic "system"
    finding dicts (see _model_branch_search_warning), one per branch that
    WAS EXPECTED to contribute (its own API key is configured) but
    actually failed. A branch whose key just isn't configured at all
    contributes nothing silently (see _search_findings_openai and
    _call_claude's own missing-key behavior) — that's an intentional,
    expected no-contribution, not a failure worth warning about; only a
    configured-but-broken branch (bad/expired key, no credit, an outage)
    produces a warning, so Александр can see directly in the report when
    a model he expects to be running actually isn't, rather than the
    ensemble silently and permanently degrading with no visible trace."""
    if model_override is not None:
        findings, cost = await _search_findings(items, target_lang, source_lang, model_override=model_override)
        return findings, cost, []

    sonnet_expected = bool(settings.ANTHROPIC_API_KEY)
    gpt_expected = bool(settings.OPENAI_API_KEY)

    ((sonnet_findings, sonnet_cost), sonnet_error), ((gpt_findings, gpt_cost), gpt_error) = await asyncio.gather(
        _run_search_branch(_search_findings(items, target_lang, source_lang)),
        _run_search_branch(_search_findings_openai(items, target_lang, source_lang)),
    )

    merged: dict[int, list[str]] = {idx: list(candidates) for idx, candidates in sonnet_findings.items()}
    for idx, candidates in gpt_findings.items():
        existing = merged.setdefault(idx, [])
        for c in candidates:
            if c not in existing:
                existing.append(c)

    warnings = []
    if sonnet_expected and sonnet_error is not None:
        warnings.append(_model_branch_search_warning("Sonnet"))
    if gpt_expected and gpt_error is not None:
        warnings.append(_model_branch_search_warning("GPT"))

    return merged, sonnet_cost + gpt_cost, warnings


# Client-specific terminology equivalence, 2026-09-24 (Александр, reporting
# real translator pushback on a Kazakh check): the platform had flagged a
# translation that rendered "отыгрыш" and "вейджер" through two different
# target-language equivalents as a meaning distortion/terminology shift.
# Александр confirmed the two Russian words are used interchangeably in his
# source texts and both mean the same betting-industry concept (a wagering
# requirement — a bonus/freebet amount that must be turned over before it
# can be withdrawn), so translating one through a term that would normally
# correspond to the other is not a real error. Not tied to any one target
# language (the source pattern is Russian, regardless of what it's being
# translated into), so this rides on the same "source is Russian" gate as
# the English-embedded-words rule below rather than living in
# GRAMMAR_LANGUAGE_HINTS.
_RU_TERM_SYNONYMS_NOTE = (
    "Отдельное уточнение от клиента: в русском исходнике слова «отыгрыш» и «вейджер» — синонимы, оба "
    "обозначают требование сделать ставки на определённую сумму, прежде чем бонус/фрибет можно вывести. Если "
    "один из этих терминов переведён через понятие, обычно соответствующее другому (например «отыгрыш» "
    "передан аналогом «вейджера» или наоборот) — это НЕ искажение смысла и не ошибка термина."
)


def _source_lang_note(source_lang: str, checks: list[str] | None = None) -> str:
    """Client-specific rule: when the source is Russian, English words or
    phrases embedded in it (brand names, terms, rare exceptions aside)
    should stay in English in every target translation too — not be
    translated into the target language. Also always appends
    _RU_TERM_SYNONYMS_NOTE (see its own comment) whenever the source is
    Russian, regardless of which checks are selected — harmless when
    "typo" isn't running (there's no finding type it could affect then).

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
        f"фрагмент всё же переведён на язык перевода, это ошибка (относи к «{category}»). "
    ) + _RU_TERM_SYNONYMS_NOTE



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


# Real translator pushback (2026-09-24, Kyrgyz + Kazakh) on findings the
# platform itself generated: before the postposition «баштап»/«бастап»
# ("начиная с"/"от"), the исходный-падеж ending attaches to the LAST
# NUMBER and depends on its own final digit/sound ("10дон баштап",
# "12ден баштап" — genuinely different endings for different numbers,
# per both translators' own explanation). That's exactly the real
# grammatical pattern the two-step pipeline was built to catch in the
# first place (see FINDINGS_SEARCH_PROMPT's own comment, and the
# "{{amount}} баштап" case that started this whole investigation) — so
# this must NOT become a blanket "never flag missing ending before
# баштап/бастап" rule, or it undoes that fix.
#
# The one case that genuinely isn't an error: when what precedes
# «баштап»/«бастап» is a TEMPLATE VARIABLE placeholder (e.g.
# {{dep_amount_currency}}) rather than a number written out in the text.
# Its actual value isn't known until runtime, so there is no correct
# ending to attach at translation time — omitting one there is a real
# grammatical necessity, not a style choice or an oversight. Deliberately
# narrow: a literal, spelled-out number (e.g. "1,25 бастап 4,0") still
# gets the platform's normal judgment, unchanged — Александр's own call
# (2026-09-24), since that case is genuinely more arguable and a blanket
# exemption there risks quietly reopening real misses instead.
GRAMMAR_LANGUAGE_HINTS: dict[str, str] = {
    "ky": (
        'Важное уточнение для этого языка (подтверждено переводчиками-носителями): перед послелогом '
        '«баштап» ("начиная с"/"от") окончание исходного падежа присоединяется к последнему числу и '
        'зависит от его звучания — «10дон баштап», «12ден баштап» и т.п. — так что это НЕ единая, всегда '
        'одинаковая форма, и разные окончания для разных чисел — это правильно, а не непоследовательность. '
        'Если вместо конкретного числа сразу перед «баштап» стоит переменная-плейсхолдер вида {{...}} — её '
        'итоговое значение на момент перевода неизвестно, поэтому окончание для неё нельзя подобрать заранее: '
        'отсутствие падежного окончания непосредственно перед «баштап» сразу после ТАКОЙ переменной — это НЕ '
        'ошибка, не сообщай о ней. Если же окончание пропущено перед «баштап» после КОНКРЕТНОГО, прямо '
        'написанного в тексте числа (не переменной) — это по-прежнему настоящая находка, здесь ничего не '
        'изменилось.\n'
    ),
    "kk": (
        'Важное уточнение для этого языка (подтверждено переводчиками-носителями): перед послелогом '
        '«бастап» ("начиная с"/"от") окончание исходного падежа (аффикс) присоединяется к последнему числу '
        'и зависит от его звучания — так что разные окончания для разных чисел — это правильно, а не '
        'непоследовательность. Если вместо конкретного числа сразу перед «бастап» стоит переменная-'
        'плейсхолдер вида {{...}} — её итоговое значение на момент перевода неизвестно, поэтому окончание '
        'для неё нельзя подобрать заранее: отсутствие падежного окончания непосредственно перед «бастап» '
        'сразу после ТАКОЙ переменной — это НЕ ошибка, не сообщай о ней. Если же окончание пропущено перед '
        '«бастап» после КОНКРЕТНОГО, прямо написанного в тексте числа (не переменной, например «1,25 бастап '
        '4,0») — по-прежнему оценивай это по общим правилам, здесь ничего не изменилось.\n'
    ),
    # Real translator pushback (2026-09-26, French), two related cases,
    # both about a multiplier phrase like "x2500 votre mise" ("x2500 your
    # stake/bet"). The platform's own default judgment (missing preposition
    # "de": should be "x2500 de votre mise") is not wrong on its own — the
    # translator confirms BOTH forms ("x2500 votre mise" and "x2500 de
    # votre mise") are actually used and acceptable in this domain, with
    # "de" being their own preferred choice, not a hard grammatical
    # requirement. So this must not become "de" is always required — only
    # that omitting it before "votre mise" (or an equivalent possessive
    # right after a bare "xN"/"x N" multiplier) is a legitimate, commonly
    # used alternative, not a punishable error, while still leaving room to
    # flag a genuinely wrong preposition elsewhere.
    "fr": (
        'Важное уточнение для этого языка (подтверждено переводчиком-носителем, реальный случай 2026-09-26): '
        'в конструкциях вида «x2500 votre mise» (умножение ставки/выигрыша, например «xN votre mise» или '
        '«x N votre mise») отсутствие предлога «de» перед притяжательным словом («votre», «leur» и т.п.) — '
        'это НЕ ошибка. И «x2500 votre mise», и «x2500 de votre mise» реально используются и оба приемлемы в '
        'этой тематике (ставки/беттинг) — «de» лишь один из допустимых вариантов, а не обязательное правило. '
        'Не сообщай о пропуске «de» именно в такой конструкции («xN»/«x N» + притяжательное слово без «de»).\n'
    ),
}


def _grammar_language_hint(target_lang: str) -> str:
    return GRAMMAR_LANGUAGE_HINTS.get(target_lang.strip().lower().split("-")[0], "")


def _target_lang_line(target_lang: str) -> str:
    """Explicitly names the target language rather than leaving the model
    to infer it purely from the translated text — closely related
    languages (e.g. Turkish/Azerbaijani, Kazakh/Kyrgyz) are otherwise a
    real risk of being mixed up, especially in short texts. Also appends
    _grammar_language_hint (normally empty) — a short, language-specific
    correction for a real grammatical pattern the model tends to
    over-flag for THIS particular language (see GRAMMAR_LANGUAGE_HINTS)."""
    code = target_lang.strip().lower()
    if not code:
        return ""
    override = LANG_CODE_MEANING_OVERRIDES.get(code.split("-")[0])
    if override:
        return (
            f"Целевой язык перевода обозначен кодом «{code}», но здесь этот код означает: {override}. "
            "Ориентируйся именно на этот язык, а не на формальное значение кода по стандарту ISO.\n"
        ) + _grammar_language_hint(target_lang)
    return (
        f"Целевой язык перевода: {code}. Ориентируйся конкретно на этот язык — не путай с родственными "
        "языками.\n"
    ) + _grammar_language_hint(target_lang)


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


# "other" — a genuinely serious problem the model notices OUTSIDE the
# selected checks. Added 2026-09-23 (Александр's ask, reviewing the
# translation-QA prompt catalog): the previous rule told the model to drop
# such a finding silently rather than force it under the wrong check type
# — good for keeping each type's own stats trustworthy, but risked a real,
# serious problem never reaching the manager at all just because it didn't
# match one of the checks ticked for that run. This keeps the "don't force
# it under the wrong type" half (a mismatched type would corrupt that
# type's own numbers) while giving a genuinely serious out-of-scope finding
# somewhere safe to land — OTHER_TYPE, kept OUT of the main checks/stats,
# clearly separate, but visible to the manager instead of silently dropped.
# Not added to CHECK_LABELS itself (that dict is what the manager opts
# INTO via checkboxes — "other" isn't opt-in, it rides along automatically
# whenever at least one real check is running, see _allowed_ai_types).
#
# Reworded 2026-09-23 alongside CALIBRATION_STRICT_OPENING/CHECK_LABELS
# above: "явно серьёзную" (explicitly SERIOUS) asked the model to clear an
# extra, undefined severity bar before it was even allowed to consider
# reporting something outside the selected checks — one more "sounds
# careful, actually just adds another reason to stay quiet" filter, in the
# same spirit as the confidence-bar wording that turned out to be
# suppressing real findings elsewhere. Replaced with "объективную" (an
# OBJECTIVE problem), matching the same objective/stylistic line
# CHECK_LABELS["typo"] now draws, rather than a separate, vaguer judgment
# call about how serious it feels.
OTHER_TYPE = "other"
_OTHER_TYPE_INSTRUCTION = (
    "Если увидишь другую объективную проблему вне этого списка (например очевидную ошибку смысла, не "
    "относящуюся ни к одному из перечисленных типов) — не подгоняй её под ближайший по смыслу разрешённый тип "
    "выше только потому, что это единственный доступный вариант: если находка не является настоящим примером "
    'именно этого критерия, её не должно быть под этим типом. Вместо этого добавь её в ответ отдельной записью '
    'с "type": "other" — так она не потеряется, но и не исказит статистику по основным критериям. Стилистические '
    "предпочтения или сомнительные наблюдения (не объективная ошибка) вне списка проверок пропускай — не сообщай "
    "о них вообще."
)


def _other_type_instruction(checks_description: str | None) -> str:
    """Empty when there's no real "Что проверять" list to be outside of in
    the first place (e.g. a register-only run) — see _checks_description's
    own None case."""
    return _OTHER_TYPE_INSTRUCTION if checks_description else ""


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


def _register_instructions(checks: list[str], batch: bool, target_lang: str = "", single_item: bool = False) -> str:
    """Empty string when "register" isn't selected, or when target_lang is
    one of NO_REGISTER_DISTINCTION_LANGS above (nothing added to the
    prompt at all either way). Otherwise, a clearly separate paragraph —
    deliberately NOT folded into the "Что проверять" problem list
    _checks_description builds — asking the model to classify the
    register actually used, for every pair that actually has one,
    regardless of whether it's "correct": this is information-gathering,
    not error-detection, so it must never be described to the model as a
    problem to avoid or a mistake to flag.

    A pair with no direct address at all (a title, a number, a technical
    label) gets NO entry at all now, rather than one tagged "neutral" —
    changed 2026-09-18 (Александр's ask): the model's own written answer
    is the expensive side of the bill, so a row with nothing to say about
    tone shouldn't still cost a full JSON entry just to say so.
    build_register_report already only ever counted "formal"/"informal"
    entries toward the majority anyway (a "neutral" entry was silently
    excluded, never a third camp) — so skipping it outright changes
    nothing about the report itself, only how many tokens it costs to get
    there.

    batch=True (BATCH_PROMPT, several pairs of one language visible
    together) asks for one entry per applicable pair, tagged by row
    number, matching that prompt's existing "row" numbering — referencing
    the "Пары для проверки" list BATCH_PROMPT shows the model. batch=False
    (SINGLE_PROMPT, exactly one pair — the standalone /check endpoint)
    asks for exactly one entry (or none) with no row number, since that
    prompt's own findings don't carry one either.

    single_item=True (only meaningful together with batch=True — see
    BATCH_PROMPT_SINGLE_ITEM/build_batch_prompt) is the third, in-between
    case: a document check with exactly one row still needs the "row": 1
    key (its findings go through the same group_batch_findings/
    _extract_register_values pipeline as a real multi-row batch, which
    keys everything off "row"/"rows"), but BATCH_PROMPT_SINGLE_ITEM has no
    "Пары для проверки" list at all to reference — added 2026-09-22 after
    review caught that referencing a nonexistent list would confuse the
    model for this exact case.

    Also appends _register_language_hint(target_lang) — normally empty,
    but a short language-specific correction for the rare case where the
    model tends to misjudge THIS particular language's own formal marker
    (see REGISTER_LANGUAGE_HINTS)."""
    if "register" not in checks or _lacks_register_distinction(target_lang):
        return ""
    if batch and single_item:
        return (
            "\nОтдельная задача, НЕ связанная с находками выше — не поиск ошибки, а сбор информации о том, "
            "как переведено на самом деле: если в переводе этой пары есть прямое обращение к пользователю, "
            "добавь в тот же JSON-массив ОДНУ дополнительную запись, строго в форме "
            f'{{"row": 1, "type": "{REGISTER_VALUE_TYPE}", "severity": "low", '
            '"value": "formal|informal|mixed", "message": ""} — value: "formal", если в ПЕРЕВОДЕ использовано '
            'обращение на «вы» (или аналог для этого языка); "informal", если на «ты»; "mixed", если В '
            'ПРЕДЕЛАХ ЭТОГО ОДНОГО перевода (он может состоять из нескольких предложений или абзацев) '
            'обращение к пользователю НЕПОСЛЕДОВАТЕЛЬНО — где-то встречается «вы», а где-то «ты», а не одна '
            'форма единообразно на протяжении всего текста. Если в переводе НЕТ прямого обращения к '
            'пользователю вообще (например, только название, число, техническая метка) — НЕ добавляй эту '
            'запись вообще, не угадывай по смыслу и не пиши никакого значения. Это НЕ находка об ошибке — не '
            "описывай её как проблему, не оценивай, правильная это форма или нет, просто зафиксируй, что "
            "реально написано в переводе (кроме значения \"mixed\" — это описание реального факта смешения "
            "форм внутри одного текста, а не оценка).\n"
        ) + _register_language_hint(target_lang)
    if batch:
        return (
            "\nОтдельная задача, НЕ связанная с находками выше — не поиск ошибки, а сбор информации о том, "
            "как переведено на самом деле: добавь в тот же JSON-массив ОДНУ дополнительную запись на КАЖДУЮ "
            "пару из списка «Пары для проверки» выше, В КОТОРОЙ в переводе есть прямое обращение к "
            "пользователю (даже если для неё нет ни одной обычной находки), строго в форме "
            f'{{"row": <номер пары>, "type": "{REGISTER_VALUE_TYPE}", "severity": "low", '
            '"value": "formal|informal|mixed", "message": ""} — value: "formal", если в ПЕРЕВОДЕ этой '
            'пары использовано обращение на «вы» (или аналог для этого языка); "informal", если на «ты»; '
            '"mixed", если В ПРЕДЕЛАХ ЭТОЙ ОДНОЙ пары (перевод может состоять из нескольких предложений или '
            'абзацев в одной ячейке) обращение к пользователю НЕПОСЛЕДОВАТЕЛЬНО — где-то встречается «вы», а '
            'где-то «ты», а не одна форма единообразно на протяжении всего текста пары. Если в переводе этой '
            'конкретной пары НЕТ прямого обращения к пользователю вообще (например, только название, число, '
            'техническая метка) — НЕ добавляй по ней запись вообще, просто пропусти эту пару, не угадывай по '
            'смыслу и не пиши для неё никакого значения. Это НЕ находка об ошибке — не описывай её как '
            "проблему, не оценивай, правильная это форма или нет, просто зафиксируй, что реально написано в "
            "переводе (кроме значения \"mixed\" — это описание реального факта смешения форм внутри одной "
            "ячейки, а не оценка).\n"
        ) + _register_language_hint(target_lang)
    return (
        "\nОтдельная задача, НЕ связанная с находками выше — не поиск ошибки, а сбор информации о том, как "
        "переведено на самом деле: если в переводе есть прямое обращение к пользователю, добавь в тот же "
        f'JSON-массив ОДНУ дополнительную запись, строго в форме {{"type": "{REGISTER_VALUE_TYPE}", '
        '"severity": "low", "value": "formal|informal|mixed", "message": ""} — value: "formal", если в '
        'переводе использовано обращение на «вы» (или аналог для этого языка); "informal", если на «ты»; '
        '"mixed", если в пределах ЭТОГО ОДНОГО перевода (он может состоять из нескольких предложений или '
        'абзацев) обращение к пользователю непоследовательно — где-то встречается «вы», а где-то «ты», а не '
        'одна форма единообразно на протяжении всего текста. Если в переводе нет прямого обращения к '
        'пользователю вообще — НЕ добавляй эту запись вообще, не угадывай по смыслу и не пиши никакого '
        "значения. Это НЕ находка об ошибке — не описывай её как проблему, не оценивай, правильная это форма "
        "или нет, просто зафиксируй, что реально написано в переводе (кроме значения \"mixed\" — это описание "
        "реального факта смешения форм внутри одного текста, а не оценка).\n"
    ) + _register_language_hint(target_lang)


def _register_array_note(checks: list[str], target_lang: str = "") -> str:
    """Appended to the "(пустой массив [] ...)" output-format line so it
    stays true once _register_instructions adds its own entries —
    without this, "пустой массив, если проблем нет" would directly
    contradict "add one entry per applicable pair" a few lines above it.
    Mirrors _register_instructions' own no-distinction-language skip (see
    NO_REGISTER_DISTINCTION_LANGS) — when no register instructions were
    actually added to the prompt, this note has nothing to justify and
    must stay empty too.

    Wording softened 2026-09-18 alongside _register_instructions' own
    skip-when-neutral change: these entries are no longer unconditionally
    mandatory for every pair, only for ones that actually have a direct
    address to report — so a run where NONE do can legitimately still
    return a genuinely empty array."""
    if "register" not in checks or _lacks_register_distinction(target_lang):
        return ""
    return (
        " — но если выбран регистр обращения, для пар с прямым обращением к пользователю такие "
        "дополнительные записи всё равно обязательны"
    )


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

    Returns None if there's nothing to report at all — no register_value
    entries came back at all (e.g. "register" wasn't selected, or the AI
    call itself failed and _extract_register_values in app.excel_multi
    never got anything to extract), OR every entry that did come back was
    something other than "formal"/"informal" (a row-by-row "mixed" is
    handled separately by the caller and never reaches here; anything
    else has nothing classifiable to report a tone for at all). Changed
    2026-09-18 (Александр's ask): this used to return a "не удалось
    определить" placeholder dict for the second case — now it's simply
    nothing to show, same as if register hadn't been asked for on that
    language at all. Otherwise a dict:
      {
        "text": <the plain-text clause this function used to return
                 directly, unprefixed/unpunctuated — still what the Excel
                 export and any other plain-text-only reader uses>,
        "majority": "formal" | "informal",
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

    `text` itself was shortened 2026-09-18 (Александр's ask) from a full
    "везде на «вы»"/"на «вы»" clause down to the bare word — "Вы"
    (capitalized, matching how the formal address is conventionally
    written on its own) or "ты" — so callers now show a terse "Тон: Вы"
    rather than a full sentence; any exceptions still ride along as a
    short "..., кроме: строка N" suffix.

    single=True drops the "везде"/"кроме" multi-row framing entirely — a
    lone pair has nothing to compare itself against, so `text` is just
    the bare word above.

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
        return None
    if single:
        only_value = next(iter(classified.values()))
        word = "Вы" if only_value == "formal" else "ты"
        return {"text": word, "majority": only_value, "exceptions": None, "exception_labels": None}

    counts: dict[str, int] = {}
    for v in classified.values():
        counts[v] = counts.get(v, 0) + 1
    majority_value = max(counts, key=lambda v: counts[v])
    majority_word = "Вы" if majority_value == "formal" else "ты"
    exception_labels = sorted(k for k, v in classified.items() if v != majority_value)
    if not exception_labels:
        return {"text": majority_word, "majority": majority_value, "exceptions": None, "exception_labels": None}

    exceptions_str = ", ".join(str(e) for e in exception_labels)
    row_word = "строка" if len(exception_labels) == 1 else "строки"
    text = f"{majority_word}, кроме: {row_word} {exceptions_str}"

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
    all — see that dict's comment — so it needs its own opt-in here.

    OTHER_TYPE is added whenever at least one real CHECK_LABELS check is
    selected (mirrors _OTHER_TYPE_INSTRUCTION's own condition for being
    added to the prompt at all — see there) — a genuinely serious problem
    the model notices outside the selected checks still needs somewhere
    safe to land, see that constant's own comment."""
    allowed = {c for c in checks if c in CHECK_LABELS}
    if allowed:
        allowed.add(OTHER_TYPE)
    if "register" in checks:
        allowed.add(REGISTER_VALUE_TYPE)
    return allowed


def _filter_findings_by_checks(findings: list[dict], checks: list[str]) -> list[dict]:
    allowed = _allowed_ai_types(checks)
    return [f for f in findings if f.get("type") in allowed]


# Languages that get the smaller MAX_ROWS_PER_AI_CALL_HARD chunk size (one
# row per AI call instead of batching several together — see
# app.excel_multi._chunk_size_for_lang) rather than a stronger model.
#
# Used to also route to CLAUDE_MODEL_HARD (Opus) instead of CLAUDE_MODEL —
# replaced wholesale on 2026-09-18 after comparing real Opus vs Sonnet
# reports side by side, dropping kazakh/uzbek/swahili/azerbaijani from the
# old list since Sonnet was already good enough for those. That per-language
# model split was RETIRED 2026-09-23 (Александр's ask): the two-step
# search-then-check pipeline below (_search_findings + the existing
# structured prompt as a second pass) turned out to close most of the real
# gap Opus was covering, for a fraction of Opus's per-token price even
# counting the extra call — see _search_findings's own comment for the
# investigation that led here. _model_for_lang now always returns
# CLAUDE_MODEL for every language; this set and _is_hard_language are kept
# only for the chunk-size decision, which is a separate, still-useful lever
# (proven necessary by the original Marathi miss) unrelated to which model
# runs. Matched against the BASE language subtag of whatever target_lang a
# check actually runs with, so "ko-KR", "ko", or any other region variant of
# Korean all get it alike. "hing" (Hinglish) isn't a real ISO code at all —
# it's this platform's own code for Hindi-English code-mixed text (see
# parse_workbook) — but the base-subtag match doesn't care, an exact "hing"
# simply matches itself.
HARD_LANGUAGE_BASES = {
    "ar", "bn", "el", "hi", "hing", "id", "ky", "ko", "mr", "ms", "ro", "te", "th", "tg", "ur",
}


def _model_for_lang(target_lang: str) -> str:
    """Always CLAUDE_MODEL (Sonnet) now — see HARD_LANGUAGE_BASES's own
    comment for why the old per-language Opus routing was retired. Kept as
    its own function (rather than inlining settings.CLAUDE_MODEL at every
    call site) so every real caller stays unaffected if a per-language
    model split is ever reintroduced, and so model_comparison's
    model_override tests still have a normal baseline to compare against."""
    return settings.CLAUDE_MODEL


def _is_hard_language(target_lang: str) -> bool:
    """Same base-subtag membership test HARD_LANGUAGE_BASES documents,
    exposed on its own so app.excel_multi's chunk-size decision (see
    MAX_ROWS_PER_AI_CALL_HARD) can key off "is this language on the hard
    list" directly. No longer tied to model choice — see
    HARD_LANGUAGE_BASES's own comment."""
    return target_lang.strip().lower().split("-")[0] in HARD_LANGUAGE_BASES


# USD per single token (not per million) — verified against
# platform.claude.com/docs/en/about-claude/pricing. Keyed by the exact
# model id, since that's what actually gets billed; if CLAUDE_MODEL or
# CLAUDE_MODEL_HARD is ever pointed at a model not listed here, cost just
# can't be computed for those calls (see _usage_cost) rather than guessing
# at a price that may no longer be current — update this table when that
# happens, or when Anthropic's prices change.
#
# Real bug Александр hit (2026-09-17): CLAUDE_MODEL on Railway had moved on
# to "claude-sonnet-5" (and this table only listed the two OLDER model ids
# above), so every check's own "Стоимость: ..." line correctly fell back to
# showing $0 — exactly the safe behavior _usage_cost's comment describes —
# while the real Anthropic bill for that same call was very much not zero
# (~$0.50 for the check he flagged). Added the two current-generation
# models below (their own prices, confirmed live against Anthropic's
# pricing page the same day) so this table covers whichever generation is
# actually configured; kept the two older entries too, since an in-flight
# Message Batch submitted before a model switch can still resolve under
# its own, older model id.
MODEL_PRICING_PER_TOKEN = {
    "claude-haiku-4-5-20251001": {"input": 1.00 / 1_000_000, "output": 5.00 / 1_000_000},
    "claude-sonnet-4-5-20250929": {"input": 3.00 / 1_000_000, "output": 15.00 / 1_000_000},
    "claude-sonnet-5": {"input": 2.00 / 1_000_000, "output": 10.00 / 1_000_000},
    "claude-opus-5": {"input": 5.00 / 1_000_000, "output": 25.00 / 1_000_000},
}
# The Message Batches API (used for large multi-checks — see
# excel_multi.BATCH_THRESHOLD_CHARS) is half price on both input and output.
BATCH_PRICE_DISCOUNT = 0.5

# OpenAI's GPT-5 family pricing, USD per single token — confirmed against
# OpenAI's own GPT-5-for-developers pricing announcement (checked
# 2026-09-23). Same "can't compute a cost for an unpriced model, degrade to
# $0 rather than guess" spirit as MODEL_PRICING_PER_TOKEN above — see
# _usage_cost's own comment for the real bug (a stale pricing table showing
# $0 while the real bill was very much not zero) that taught us to keep
# these tables honest rather than silently stale. Update this table if
# OPENAI_MODEL is ever pointed at a model not listed here, or when OpenAI's
# prices change.
OPENAI_MODEL_PRICING_PER_TOKEN = {
    "gpt-5-mini": {"input": 0.25 / 1_000_000, "output": 2.00 / 1_000_000},
    "gpt-5-nano": {"input": 0.05 / 1_000_000, "output": 0.40 / 1_000_000},
    "gpt-5": {"input": 1.25 / 1_000_000, "output": 10.00 / 1_000_000},
}


def _openai_usage_cost(model: str, usage: dict | None) -> float:
    """Same idea as _usage_cost above, but OpenAI's usage dict uses
    prompt_tokens/completion_tokens instead of Anthropic's own
    input_tokens/output_tokens — kept as its own function rather than
    reshaping OpenAI's usage dict to fit _usage_cost, so each stays a
    direct, readable match for its own vendor's real response shape."""
    rates = OPENAI_MODEL_PRICING_PER_TOKEN.get(model)
    if not rates or not usage:
        return 0.0
    return usage.get("prompt_tokens", 0) * rates["input"] + usage.get("completion_tokens", 0) * rates["output"]


# Anthropic prompt caching (2026-09-25, Александр's cost-cutting ask — see
# _call_claude's own cache_prefix parameter): a cache WRITE (the first call
# to send a given prefix, or any call after the ~5-minute window lapses)
# costs 25% MORE than a normal input token; a cache READ (a later call
# reusing that exact prefix within the window) costs only 10%. Both are
# reported separately from plain "input_tokens" in Anthropic's usage dict —
# only present at all when a call actually used cache_control — so
# _usage_cost below has to add them in on top of the existing input/output
# math, not instead of it.
#
# Applied flat across every model in MODEL_PRICING_PER_TOKEN, which slightly
# OVERSTATES the displayed cost for CLAUDE_MODEL_HARD/Opus specifically —
# Anthropic's docs (checked 2026-09-25) price a cache READ on Opus at 5% of
# input, not this table's flat 10%, and didn't confirm whether that applies
# to plain "claude-opus-5" or only a later Opus point release. Same spirit
# as this function's own "unpriced model degrades to $0 rather than guessing
# wrong in either direction" — here the real Anthropic bill is completely
# unaffected either way (this only feeds the "Стоимость: ..." number shown
# in the report), and erring toward a SMALLER shown discount felt safer than
# risking the opposite of the real bug Александр hit on 2026-09-17 (this
# number confidently showing a saving that turned out not to be real).
CACHE_WRITE_PRICE_MULTIPLIER = 1.25
CACHE_READ_PRICE_MULTIPLIER = 0.1


def _usage_cost(model: str, usage: dict | None, batch: bool = False) -> float:
    """USD cost of one API call from its token usage. Returns 0.0 (rather
    than raising) for an unpriced model or missing usage, so a pricing-table
    gap degrades to "cost not shown" instead of breaking the check itself."""
    rates = MODEL_PRICING_PER_TOKEN.get(model)
    if not rates or not usage:
        return 0.0
    cost = usage.get("input_tokens", 0) * rates["input"] + usage.get("output_tokens", 0) * rates["output"]
    cost += usage.get("cache_creation_input_tokens", 0) * rates["input"] * CACHE_WRITE_PRICE_MULTIPLIER
    cost += usage.get("cache_read_input_tokens", 0) * rates["input"] * CACHE_READ_PRICE_MULTIPLIER
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


async def _call_claude(
    prompt: str, model: str | None = None, cache_prefix: str | None = None,
) -> tuple[str | None, dict, str | None]:
    """Returns (response_text, usage, stop_reason) — usage is Anthropic's raw
    {"input_tokens": int, "output_tokens": int, ...} dict (empty when no API
    key is configured), used by callers to compute and surface this check's
    actual API cost. stop_reason is "max_tokens" when the response was cut
    off mid-generation (the response is then incomplete/truncated JSON) —
    callers use this to warn rather than silently show a partial result as
    if it were complete.

    cache_prefix (2026-09-25, Александр's cost-cutting ask): when given, MUST
    be an exact leading substring of `prompt` (asserted below) — the fixed,
    non-row-specific instruction text a caller has already identified as
    byte-identical across many calls (see each *_PREFIX/*_SUFFIX prompt split
    above BATCH_PROMPT, SINGLE_PROMPT, etc.). Sent as its own content block
    with Anthropic's prompt-caching flag, so the model reads EXACTLY the same
    text either way (cache_prefix + prompt[len(cache_prefix):] == prompt) —
    this only changes how the request is billed, never what's in it. A call
    that reuses the same cache_prefix within roughly 5 minutes of a prior one
    is billed ~10% of the normal price for that block instead of full price;
    the very first call (or one after the window lapses) pays a small ~25%
    premium on just that block to write it into the cache. Below Anthropic's
    minimum cacheable block size (confirmed against Anthropic's own docs,
    2026-09-25: 1024 tokens for CLAUDE_MODEL/Sonnet, only 512 for
    CLAUDE_MODEL_HARD/Opus — Opus's lower minimum is a small extra point in
    favor of caching working out for the hard-language path specifically),
    cache_control is silently a no-op — billed as ordinary input, no premium
    and no discount — so passing a short cache_prefix here is always safe,
    just not always a win. None (the default) sends `prompt` as a single
    block, unchanged from before this parameter existed."""
    if not settings.ANTHROPIC_API_KEY:
        return None, {}, None
    resolved_model = model or settings.CLAUDE_MODEL
    if cache_prefix:
        assert prompt.startswith(cache_prefix), "cache_prefix must be an exact leading substring of prompt"
        content: str | list[dict] = [
            {"type": "text", "text": cache_prefix, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": prompt[len(cache_prefix):]},
        ]
    else:
        content = prompt
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
                "messages": [{"role": "user", "content": content}],
            },
        )
        resp.raise_for_status()
        data = resp.json()

    text = next((b["text"] for b in data.get("content", []) if b.get("type") == "text"), None)
    return text, data.get("usage", {}), data.get("stop_reason")


async def _call_openai(prompt: str, model: str | None = None) -> tuple[str | None, dict, str | None]:
    """OpenAI equivalent of _call_claude, called via plain REST (same style
    as the Anthropic calls in this file) rather than the openai SDK — no
    new dependency needed, and every other API call here already talks to
    its vendor directly over httpx. Returns (None, {}, None) with no
    request at all when no OPENAI_API_KEY is configured — mirrors
    _call_claude's own missing-key behavior, so a caller can treat "no GPT
    key" and "no Anthropic key" identically. finish_reason "length" (GPT's
    own name for a response cut off at the token ceiling) is normalized to
    "max_tokens" here so it reads the same as _call_claude's stop_reason,
    even though Step 1's own caller (_search_findings_openai) doesn't
    currently act on it — kept consistent in case a future caller does."""
    if not settings.OPENAI_API_KEY:
        return None, {}, None
    resolved_model = model or settings.OPENAI_MODEL
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "content-type": "application/json",
            },
            json={
                "model": resolved_model,
                "max_completion_tokens": AI_MAX_TOKENS,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        data = resp.json()

    choice = (data.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content")
    finish_reason = choice.get("finish_reason")
    stop_reason = "max_tokens" if finish_reason == "length" else finish_reason
    return text, data.get("usage", {}), stop_reason


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

    # Step 1 of the two-step pipeline (see FINDINGS_SEARCH_PROMPT's own
    # comment) — a single-pair "items" list of one, so _search_findings'
    # shared plumbing (same one run_ai_checks_batch below uses) works
    # unchanged here too. Costs an extra API call every time, which is the
    # whole point (Александр's ask, 2026-09-23) — summed into this
    # function's own returned cost_usd below. Only worth running when
    # there's an actual "Что проверять" list to search against — a
    # register-ONLY run (checks_description empty, register_instructions
    # not) has nothing for a free error search to even look for, so it's
    # skipped there rather than spending a whole extra call finding nothing
    # relevant, exactly like build_batch_prompt/_search_findings's own
    # "nothing checkable" early-outs.
    prior_findings: dict[int, list[str]] = {}
    search_cost = 0.0
    search_warnings: list[dict] = []
    if checks_description:
        prior_findings, search_cost, search_warnings = await _ensemble_search_findings(
            [{"context": "", "source": source, "translation": translation}],
            target_lang=target_lang, source_lang=source_lang,
        )

    prompt_kwargs = dict(
        target_lang_line=_target_lang_line(target_lang),
        calibration=_calibration(checks),
        source_lang_note=_source_lang_note(source_lang, checks),
        source=source,
        translation=translation,
        prior_findings=_prior_findings_block(prior_findings.get(0)),
        extra_instructions=extra_instructions.strip() or "нет",
        checks_description=checks_description or "(нет — только сбор информации о регистре обращения ниже)",
        other_type_instruction=_other_type_instruction(checks_description),
        register_instructions=register_instructions,
        register_array_note=_register_array_note(checks, target_lang=target_lang),
        type_enum="|".join(sorted(_allowed_ai_types(checks))),
    )
    # cache_prefix: see _SINGLE_PROMPT_PREFIX's own comment — fixed per
    # (target_lang, checks), independent of this specific pair's text.
    cache_prefix = _SINGLE_PROMPT_PREFIX.format(**prompt_kwargs)
    prompt = cache_prefix + _SINGLE_PROMPT_SUFFIX.format(**prompt_kwargs)
    model = _model_for_lang(target_lang)
    text_block, usage, stop_reason = await _call_claude(prompt, model=model, cache_prefix=cache_prefix)
    findings = _filter_findings_by_checks(parse_json_array(text_block), checks)
    if stop_reason == "max_tokens":
        findings = findings + [_truncation_warning()]
    findings = findings + search_warnings

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
                    "message": f"Тон: {report['text']}.",
                    "register_majority": report["majority"],
                })

    return findings, search_cost + _usage_cost(model, usage)


def build_batch_prompt(
    items: list[dict],
    checks: list[str],
    extra_instructions: str = "",
    target_lang: str = "",
    source_lang: str = "",
    prior_findings: dict[int, list[str]] | None = None,
) -> tuple[str | None, str | None, dict[int, int]]:
    """
    Builds the prompt for one language's batch of (context, source,
    translation) triples, without calling the API — shared by the
    synchronous path (run_ai_checks_batch, below) and the Message Batches
    path (excel_multi.build_batch_plan), so both send an identical prompt
    for the same input.

    items: list of {"context": str, "source": str, "translation": str}, all
    in the same target language. Items with an empty translation are
    skipped (handled by rule checks as "missing translation" instead).

    prior_findings: optional Step 1 candidates (see _search_findings),
    keyed by the ORIGINAL index into `items` — the same key space
    run_ai_checks_batch's own return value and _search_findings's return
    value both use. Embedded per-pair via _pairs_block/_prior_findings_block
    (multi-item case) or as this call's own {prior_findings} placeholder
    (single-item case, via BATCH_PROMPT_SINGLE_ITEM). None/omitted keeps
    the prompt byte-for-byte what it was before the two-step pipeline
    existed — every non-two-step caller (excel_multi.build_batch_plan's
    Message Batches path, still single-step — see its own module comment)
    is unaffected by this parameter's existence.

    Returns (prompt, cache_prefix, number_to_index) — prompt is None when
    there's nothing to ask the AI (no AI check types selected, or nothing
    checkable), and cache_prefix is None right along with it. cache_prefix
    (2026-09-25, Александр's cost-cutting ask) is the leading substring of
    `prompt` that's fixed for this whole document/run regardless of which
    rows ended up in THIS particular chunk (see _BATCH_PROMPT_PREFIX/
    _BATCH_PROMPT_SINGLE_ITEM_PREFIX's own comments) — pass it straight
    through to _call_claude's own cache_prefix parameter; a caller that
    doesn't (the Message Batches path, or a diagnostic like
    model_comparison.py) can simply ignore it, `prompt` alone is still the
    exact same text as before this parameter existed. number_to_index maps
    the 1-based "row" numbers used inside the prompt back to the caller's
    original item indices — pass it to group_batch_findings once you have
    the model's response.
    """
    checks_description = _checks_description(checks)
    # Just a truthiness probe here (is there anything to ask the AI at
    # all?) — single_item doesn't affect WHETHER this is empty, only its
    # exact wording once we know len(checkable), so the real value used in
    # the prompt is recomputed below with the correct single_item flag.
    if not checks_description and not _register_instructions(checks, batch=True, target_lang=target_lang):
        return None, None, {}

    checkable = _checkable_items(items)
    if not checkable:
        return None, None, {}

    is_single_item = len(checkable) == 1
    register_instructions = _register_instructions(
        checks, batch=True, target_lang=target_lang, single_item=is_single_item,
    )
    common_kwargs = dict(
        target_lang_line=_target_lang_line(target_lang),
        calibration=_calibration(checks),
        source_lang_note=_source_lang_note(source_lang, checks),
        extra_instructions=extra_instructions.strip() or "нет",
        checks_description=checks_description or "(нет — только сбор информации о регистре обращения ниже)",
        other_type_instruction=_other_type_instruction(checks_description),
        register_instructions=register_instructions,
        register_array_note=_register_array_note(checks, target_lang=target_lang),
        type_enum="|".join(sorted(_allowed_ai_types(checks))),
    )
    if is_single_item:
        # See BATCH_PROMPT_SINGLE_ITEM's own comment — skips the whole
        # cross-row-duplicate instruction block, which is meaningless (and,
        # per Александр's real test, apparently costly to accuracy) when
        # there's only one pair to look at in the first place.
        only_idx, only_item = checkable[0]
        cache_prefix = _BATCH_PROMPT_SINGLE_ITEM_PREFIX.format(**common_kwargs)
        prompt = cache_prefix + _BATCH_PROMPT_SINGLE_ITEM_SUFFIX.format(
            context=only_item["context"] or "—",
            source=only_item["source"],
            translation=only_item["translation"],
            prior_findings=_prior_findings_block((prior_findings or {}).get(only_idx)),
            **common_kwargs,
        )
    else:
        pairs_block = _pairs_block(checkable, prior_findings)
        cache_prefix = _BATCH_PROMPT_PREFIX.format(**common_kwargs)
        prompt = cache_prefix + _BATCH_PROMPT_SUFFIX.format(pairs_block=pairs_block, **common_kwargs)
    number_to_index = {n: idx for n, (idx, _) in enumerate(checkable, start=1)}
    return prompt, cache_prefix, number_to_index


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
    model_override: str | None = None,
) -> tuple[dict[int, list[dict]], float, bool, list[dict]]:
    """Synchronous path: builds the prompt, calls Claude right away, and
    returns (findings keyed by index into items, this call's cost_usd,
    whether the response was truncated by the max_tokens ceiling, Step 1's
    own ensemble search_warnings — see _ensemble_search_findings). The
    caller adds a visible warning for the truncation/search_warnings cases
    rather than presenting a partial or silently-degraded result as a
    complete one.

    model_override: bypass the normal _model_for_lang(target_lang)
    selection and force a specific model id instead. Added 2026-09-22
    for app.model_comparison's diagnostic tool only (see its own
    comment) — every real production caller leaves this None and gets
    the normal per-language model choice, unaffected.

    Findings keyed by index here still include any REGISTER_VALUE_TYPE
    entries mixed in with real findings — app.excel_multi extracts and
    summarizes those itself (it's the one with the excel_row numbers to
    label them with), not this function.

    This is the LIVE path — app.excel_multi's _run_ai_chunks calls this for
    every upload under the Message Batches size threshold, and run_ai_checks
    above calls the single-pair equivalent — so it's the one that got the
    two-step pipeline (see FINDINGS_SEARCH_PROMPT's own comment). The async
    Message Batches path (excel_multi.build_batch_plan, for large uploads)
    still calls build_batch_prompt directly with no prior_findings — Step 1
    needing its own full submit-and-poll round there too (on top of Step
    2's) makes that a separate, bigger change, deliberately deferred."""
    checks_description = _checks_description(checks)
    register_instructions = _register_instructions(checks, batch=True, target_lang=target_lang)
    if not checks_description and not register_instructions:
        return {}, 0.0, False, []

    # Step 1 — same rationale as run_ai_checks's own call to this,
    # including skipping it entirely for a register-only run (nothing for
    # a free error search to look for — see run_ai_checks's own comment on
    # this same guard). Passed the same model_override as Step 2 below, so
    # a caller forcing a specific model gets that model for both steps
    # rather than Step 1 quietly running under _model_for_lang's normal
    # pick instead.
    prior_findings: dict[int, list[str]] = {}
    search_cost = 0.0
    search_warnings: list[dict] = []
    if checks_description:
        prior_findings, search_cost, search_warnings = await _ensemble_search_findings(
            items, target_lang=target_lang, source_lang=source_lang, model_override=model_override,
        )

    prompt, cache_prefix, number_to_index = build_batch_prompt(
        items, checks, extra_instructions, target_lang, source_lang, prior_findings=prior_findings,
    )
    if prompt is None:
        return {}, search_cost, False, search_warnings
    model = model_override or _model_for_lang(target_lang)
    text_block, usage, stop_reason = await _call_claude(prompt, model=model, cache_prefix=cache_prefix)
    raw = parse_json_array(text_block)
    grouped = group_batch_findings(raw, number_to_index)
    filtered = {idx: _filter_findings_by_checks(fs, checks) for idx, fs in grouped.items()}
    return filtered, search_cost + _usage_cost(model, usage), stop_reason == "max_tokens", search_warnings


# ------------------------------------------------ automatic second opinion ---
# Александр's ask (2026-09-25): after a multi-check finishes, automatically
# send each language's already-reported findings — numbered, one language at
# a time — to BOTH Sonnet and GPT for an independent opinion on how likely
# each one is a real problem, so the report page can offer a "Отфильтровать
# отчёт" button that drops the findings neither model is convinced by. This
# is exactly what he was doing BY HAND (see frontend copyReport.ts: copying
# the report and pasting it into a chat) — automated, and run whole-language-
# at-once like that manual flow, NOT like the old Step 3 (see the "Revert
# Step 3" commit), whose isolated single-row AI calls for hard languages
# produced inconsistent percentages for the exact same repeated issue. A
# model reviewing the whole numbered list at once can actually notice and
# score repeats consistently.
#
# Deliberately asks for a percent only, nothing else — the finding's own
# existing "message" (already shown in the report) doubles as the
# "Комментарий" column Александр wants, so there's no second AI-authored
# explanation to generate, parse, or trust.

# Split into PREFIX/SUFFIX — same reasoning as the other four prompts'
# splits above. This one is the cleanest case: literally everything except
# the trailing {numbered_report} is fixed, full stop — not just per
# language but across the ENTIRE app, every run, every language, forever.
# run_second_opinion fires this once per language (not per chunk), but all
# ~30 languages in one multi-check go out together via asyncio.gather, well
# within Anthropic's cache window — so this prefix should hit cache on
# language #2 onward in the very same run. SECOND_OPINION_PROMPT itself is
# still the exact same text as before.
_SECOND_OPINION_PROMPT_PREFIX = """Ниже — пронумерованный список находок по одному языку при проверке качества перевода. У каждой находки указан контекст (строка, источник, перевод) и описание проблемы.

Оцени вероятность того, что каждая находка — реальная проблема, а не нормальный вариант перевода, устоявшийся термин, региональная особенность или ошибка самой проверки. Используй шкалу:
- 90-100 — явная фактическая или техническая ошибка: перепутана цифра, валюта, единица измерения, потерян или искажён плейсхолдер, опечатка, искажён смысл.
- 60-89 — ошибка вероятна, либо это неконсистентность в переводе: один и тот же термин в разных местах переведён по-разному без причины. Оба варианта по отдельности могут быть правильными, но вместе — непоследовательность, которую стоит исправить.
- 30-59 — скорее вопрос стиля или личного предпочтения, не критично.
- 0-29 — похоже на нормальный, допустимый вариант перевода, вероятно ложное срабатывание.

Ответь ТОЛЬКО валидным JSON-массивом, без какого-либо текста до или после, в формате:
[{{"n": 1, "percent": 85}}, {{"n": 2, "percent": 20}}]

Каждому номеру находки из списка ниже должен соответствовать ровно один объект в массиве.

Находки:
"""
_SECOND_OPINION_PROMPT_SUFFIX = """{numbered_report}"""
SECOND_OPINION_PROMPT = _SECOND_OPINION_PROMPT_PREFIX + _SECOND_OPINION_PROMPT_SUFFIX


def _second_opinion_input(rows: list[dict]) -> tuple[str, list[dict]]:
    """Builds the numbered plain-text block to send for scoring, and the
    parallel list of finding dicts each number refers to (finding_refs[i]
    is what number i+1 refers to). Only real findings whose type ISN'T one
    of rule_checks.RULE_BASED_TYPES are included — those are scored 100/100
    directly in code by run_second_opinion below, never sent to a model at
    all, so they can't come back scored any other way. register_summary
    (the "Тон обращения" fact) and excel_row==0 system rows are never
    findings needing a validity opinion, so both are skipped entirely, same
    as the frontend's manual copy-report numbering (copyReport.ts)."""
    lines: list[str] = []
    finding_refs: list[dict] = []
    for row in rows:
        if row["excel_row"] == 0:
            continue
        scoreable = [
            f for f in row["findings"]
            if f.get("type") not in RULE_BASED_TYPES and f.get("type") != "register_summary"
        ]
        if not scoreable:
            continue
        lines.append(f"Строка {row['excel_row']} — {row.get('context') or 'без контекста'}")
        lines.append(f"Источник: {row['source']}")
        lines.append(f"Перевод: {row['translation']}")
        for f in scoreable:
            finding_refs.append(f)
            lines.append(f"{len(finding_refs)}. {f.get('message', '')}")
        lines.append("")
    return "\n".join(lines).strip(), finding_refs


def _parse_second_opinion(raw: list) -> dict[int, int]:
    """{finding number: percent clamped to 0-100}, silently skipping any
    entry that doesn't parse cleanly (wrong shape, non-numeric percent)
    rather than guessing — a finding number missing from the result just
    ends up with no percent from this model, which run_second_opinion's own
    caller treats as "can't be filtered, always keep" (see its docstring),
    never as a 0."""
    out: dict[int, int] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        n, percent = entry.get("n"), entry.get("percent")
        if not isinstance(n, int) or not isinstance(percent, (int, float)) or isinstance(percent, bool):
            continue
        out[n] = max(0, min(100, int(percent)))
    return out


def _second_opinion_failure_warning(model_label: str) -> dict:
    """Same synthetic "type": "system" finding pattern as
    _model_branch_search_warning above, but for THIS step — surfaced only
    when a model that WAS configured (its API key is set) failed to
    contribute a second opinion for this language, so Александр can tell
    "this model genuinely reviewed and found nothing to remove" apart from
    "this model's review didn't run at all". A model whose key simply isn't
    configured contributes silently, same as Step 1's own ensemble — that's
    an expected, intentional non-contribution, not a failure."""
    return {
        "type": "system",
        "severity": "medium",
        "message": (
            f"Автоматическая повторная проверка находок этого языка не сработала для модели {model_label} "
            "(сбой на её стороне — например, закончились средства на счёте, неверный/просроченный ключ API, "
            "временная недоступность сервиса). Находки, для которых нет оценки от этой модели, при нажатии "
            "«Отфильтровать отчёт» останутся в отчёте — они не будут убраны без данных от обеих моделей."
        ),
    }


def _second_opinion_unexpected_error_warning() -> dict:
    """Same synthetic-finding pattern as _second_opinion_failure_warning,
    but for app.excel_multi.apply_second_opinion's own catch-all — an
    unexpected bug in this step (not a model API call failing, which
    run_second_opinion already handles per-branch) must never take the
    whole check down with it, but it also shouldn't fail completely
    silently. Findings for this language simply keep no sonnet_percent/
    gpt_percent at all (same "can't be filtered, always keep" fallback as
    a single failed model branch)."""
    return {
        "type": "system",
        "severity": "medium",
        "message": (
            "Автоматическая повторная проверка находок этого языка не выполнилась из-за непредвиденной "
            "ошибки. Остальная часть проверки отработала нормально — эта проблема касается только "
            "дополнительной оценки вероятности ошибки для второго мнения (Sonnet/GPT). Находки этого "
            "языка при нажатии «Отфильтровать отчёт» останутся в отчёте без изменений."
        ),
    }


async def run_second_opinion(rows: list[dict]) -> tuple[float, list[dict]]:
    """Attaches sonnet_percent/gpt_percent (0-100 ints) to every real,
    AI-judged finding across rows, in place. Algorithmic findings
    (rule_checks.RULE_BASED_TYPES) are set to 100/100 directly here without
    ever being sent to a model — see _second_opinion_input's own comment for
    why. A finding whose type a model wasn't asked about, or whose number
    didn't come back in a model's response at all, simply keeps that
    model's percent unset.

    Returns (extra cost_usd this added, warning findings — one per model
    that was expected to contribute but failed; see
    _second_opinion_failure_warning). Returns (0.0, []) immediately when
    there's nothing to score for this language at all."""
    for row in rows:
        if row["excel_row"] == 0:
            continue
        for f in row["findings"]:
            if f.get("type") in RULE_BASED_TYPES:
                f["sonnet_percent"] = 100
                f["gpt_percent"] = 100

    numbered_report, finding_refs = _second_opinion_input(rows)
    if not finding_refs:
        return 0.0, []

    # cache_prefix: see _SECOND_OPINION_PROMPT_PREFIX's own comment — this
    # one is fully static (no per-call fields at all), so it's the same
    # constant across every language in every run, forever.
    cache_prefix = _SECOND_OPINION_PROMPT_PREFIX
    prompt = cache_prefix + _SECOND_OPINION_PROMPT_SUFFIX.format(numbered_report=numbered_report)
    sonnet_expected = bool(settings.ANTHROPIC_API_KEY)
    gpt_expected = bool(settings.OPENAI_API_KEY)

    async def _sonnet() -> tuple[dict[int, int], float]:
        text_block, usage, _ = await _call_claude(prompt, model=settings.CLAUDE_MODEL, cache_prefix=cache_prefix)
        return _parse_second_opinion(parse_json_array(text_block)), _usage_cost(settings.CLAUDE_MODEL, usage)

    async def _gpt() -> tuple[dict[int, int], float]:
        text_block, usage, _ = await _call_openai(prompt, model=settings.OPENAI_MODEL)
        return _parse_second_opinion(parse_json_array(text_block)), _openai_usage_cost(settings.OPENAI_MODEL, usage)

    ((sonnet_percents, sonnet_cost), sonnet_error), ((gpt_percents, gpt_cost), gpt_error) = await asyncio.gather(
        _run_search_branch(_sonnet()), _run_search_branch(_gpt()),
    )

    for n, f in enumerate(finding_refs, start=1):
        if n in sonnet_percents:
            f["sonnet_percent"] = sonnet_percents[n]
        if n in gpt_percents:
            f["gpt_percent"] = gpt_percents[n]

    warnings = []
    if sonnet_expected and sonnet_error is not None:
        warnings.append(_second_opinion_failure_warning("Sonnet"))
    if gpt_expected and gpt_error is not None:
        warnings.append(_second_opinion_failure_warning("GPT"))
    return sonnet_cost + gpt_cost, warnings


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


def _batch_request_content(prompt: str, cache_prefix: str | None) -> str | list[dict]:
    """Same prefix/suffix content-block split _call_claude's own cache_prefix
    parameter does for the live path — confirmed against Anthropic's own
    docs (2026-09-25) that the Message Batches API accepts cache_control the
    same way a regular request does, AND that the cache is shared workspace-
    wide between batch and non-batch calls (a batch request can hit a cache
    a live call wrote moments earlier, and vice versa) — not isolated to
    "requests inside the same batch job" the way it might have been assumed.
    Below Anthropic's per-model minimum cacheable block size, this is a
    silent no-op (billed as ordinary input, same as _call_claude's own
    cache_prefix), so passing one is always safe."""
    if not cache_prefix:
        return prompt
    assert prompt.startswith(cache_prefix), "cache_prefix must be an exact leading substring of prompt"
    return [
        {"type": "text", "text": cache_prefix, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": prompt[len(cache_prefix):]},
    ]


async def create_message_batch(requests: list[dict]) -> str | None:
    """requests: list of {"custom_id": str, "prompt": str, "model": str
    (optional), "cache_prefix": str (optional)}. Submits them all as one
    Anthropic Message Batch — each request can specify its own model (see
    excel_multi.build_batch_plan, which sets the per-language model via
    _model_for_lang), falling back to the default CLAUDE_MODEL when omitted
    — and returns the batch id, or None if there's no API key configured or
    nothing to submit. cache_prefix, when a request has one, is split out
    into its own cached content block (see _batch_request_content) — purely
    a billing optimization, changes nothing about what's in the request."""
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
                "messages": [{"role": "user", "content": _batch_request_content(r["prompt"], r.get("cache_prefix"))}],
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
