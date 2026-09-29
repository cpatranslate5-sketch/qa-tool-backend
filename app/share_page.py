"""Public report page for translators (2026-09-29; redesigned 2026-09-30
and 2026-10-01 — Александр).

Opened by a share link (see models.ShareLink): ONE language of ONE
multi-check report. Two stages on the same page:

1. Head of QA («руководитель ОКК»): every finding the manager marked ✓
   (light green) or ? (light yellow) waits with «Оставить переводчику» /
   «Убрать». The manager's note is meant for the QA head only — shown in red,
   read-only. The QA head can write a «Комментарий для переводчика».
2. Translator: a kept finding shows its number, source, translation, Crowdin
   link(s), the platform's comment and the QA head's comment (red), a field
   for the translator's own comment (red) and «Правка внесена» / «Не
   актуально». Once answered, a checkbox appears in the top-right corner
   for the manager who verifies the edits.

No confidence percents, costs, model names, folder or project names, and no
way to navigate anywhere else: the only links are the manager's own Crowdin
links, and the one inline script (allowed by a per-request nonce) can talk
only to this same server. Rendered live on every request.
"""
import html
import re

_URL_RE = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)


def share_page_headers(nonce: str) -> dict:
    """Response headers that lock the page down: only our own inline script
    (by nonce) may run and it may only call this server; the page can't be
    framed, isn't indexed, and opening a Crowdin link doesn't leak this
    page's address to the other site."""
    script = f"script-src 'nonce-{nonce}'; connect-src 'self'; " if nonce else ""
    return {
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; " + script
            + "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        ),
        "X-Robots-Tag": "noindex, nofollow",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Cache-Control": "no-store",
    }


_CSS = """
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 24px 16px 60px; background: #f5f6f8; color: #1c2230;
         font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; }
  .page { max-width: 880px; margin: 0 auto; }
  h1 { font-size: 1.35rem; margin: 0 0 4px; }
  .muted { color: #6b7280; font-size: 0.88rem; }
  .label { font-weight: 700; color: #3949ab; }
  .num { font-weight: 700; color: #111; font-size: 0.95rem; margin-bottom: 6px; }
  .red { color: #dc2626; white-space: pre-wrap; }
  .tag { color: #c026d3; font-weight: 600; }
  .item { position: relative; background: #fff; border: 1px solid #dde1e7; border-radius: 10px; padding: 12px 14px; margin-top: 12px;
          transition: background .15s, border-color .15s; }
  .item.pending.p-accept { background: #eaf7ef; border-color: #9fd5b3; }
  .item.pending.p-question { background: #fff8db; border-color: #f0d98c; }
  .item.done { background: #eaf7ef; border-color: #9fd5b3; }
  .item.na { background: #fdeeee; border-color: #f0b4b4; }
  .field { font-size: 0.9rem; margin-top: 4px; }
  .links a { color: #4f46e5; word-break: break-all; display: block; }
  .comment { border-left: 3px solid #d98a1f; background: rgba(0,0,0,0.03); padding: 6px 10px; margin-top: 8px; font-size: 0.9rem; }
  .block-label { display: block; font-size: 0.85rem; margin-top: 10px; }
  textarea { width: 100%; min-height: 54px; margin-top: 4px; font: inherit; font-size: 0.88rem; padding: 6px 8px;
             border: 1px solid #dde1e7; border-radius: 6px; resize: vertical; background: #fff; color: #dc2626; }
  textarea.okk-links { color: #4f46e5; min-height: 40px; }
  .actions { display: flex; justify-content: flex-end; align-items: center; gap: 8px; margin-top: 10px; }
  .status { font-size: 0.8rem; color: #6b7280; margin-right: auto; }
  .btn { border: 1px solid; border-radius: 8px; padding: 6px 16px; font: inherit; font-size: 0.88rem; cursor: pointer; background: #fff; }
  .btn-keep, .btn-done { color: #17703c; border-color: #17703c; }
  .btn-remove, .btn-na { color: #b42318; border-color: #b42318; }
  .item.done .btn-done { background: #17703c; color: #fff; }
  .item.na .btn-na { background: #b42318; color: #fff; }
  .check { position: absolute; top: 10px; right: 12px; display: none; align-items: center; gap: 6px;
           font-size: 0.8rem; color: #374151; background: #fff; border: 1px solid #dde1e7; border-radius: 6px; padding: 3px 8px; cursor: pointer; }
  .item.done .check, .item.na .check { display: flex; }
  .check input { width: 16px; height: 16px; margin: 0; cursor: pointer; }
  .empty { background: #fff; border: 1px solid #dde1e7; border-radius: 10px; padding: 16px; margin-top: 14px; }
"""


def _e(v) -> str:
    return html.escape(str(v or ""), quote=True)


# Tags/placeholders in texts ({name}, <b>, %s, [link]…) are colored so they
# stand out — same pattern as app.rule_checks.PLACEHOLDER_RE.
from app.rule_checks import PLACEHOLDER_RE as _TAG_RE


def _t(v) -> str:
    """Escaped text with its tags wrapped in <span class="tag">."""
    s = str(v or "")
    out, last = [], 0
    for m in _TAG_RE.finditer(s):
        out.append(_e(s[last:m.start()]))
        out.append(f'<span class="tag">{_e(m.group(0))}</span>')
        last = m.end()
    out.append(_e(s[last:]))
    return "".join(out)


# Row numbers mean nothing to translators (Crowdin splits strings its own
# way — Александр, 2026-10-01), so the translator page never shows them.
_ALSO_ROWS_RE = re.compile(r"\s*\(также в строках:[^)]*\)\s*$")


def _strip_rows(message: str) -> str:
    return _ALSO_ROWS_RE.sub("", str(message or ""))


def _tone_text(f: dict) -> str:
    """The tone summary without row numbers: «Вы», «ты», or the majority
    plus the actual exception texts (when the check kept them)."""
    majority = f.get("register_majority")
    word = "Вы" if majority == "formal" else "ты" if majority == "informal" else ""
    if not word:
        return re.sub(r",?\s*кроме:.*$", "", _strip_rows(f.get("message"))).rstrip(".") or ""
    exceptions = f.get("register_exceptions") or []
    texts = [str(e.get("text") or "").strip() for e in exceptions if str(e.get("text") or "").strip()]
    if texts:
        return f"{word}, кроме: " + "; ".join(f"«{t}»" for t in texts)
    if exceptions or f.get("register_exception_labels"):
        return f"{word} (есть отдельные исключения)"
    return word


def _links_html(raw: str) -> str:
    parts = [p for p in re.split(r"\s+", raw or "") if p]
    if not parts:
        return ""
    items = []
    for p in parts:
        if _URL_RE.match(p):
            items.append(f'<a href="{_e(p)}" target="_blank" rel="noopener noreferrer">{_e(p)}</a>')
        else:
            items.append(f"<span>{_e(p)}</span>")
    return '<div class="field links"><span class="label">Ссылки на Crowdin:</span>' + "".join(items) + "</div>"


def _page(title: str, body: str, script: str = "", nonce: str = "") -> str:
    tail = f'<script nonce="{_e(nonce)}">{script}</script>' if script and nonce else ""
    return (
        '<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />'
        '<meta name="robots" content="noindex, nofollow" />'
        f"<title>{_e(title)}</title><style>{_CSS}</style></head>"
        f'<body><div class="page">{body}</div>{tail}</body></html>'
    )


def render_not_found() -> str:
    return _page("Ссылка недействительна", "<h1>Ссылка недействительна</h1>"
                 '<p class="muted">Эта ссылка отключена или не существует. Обратитесь к менеджеру.</p>')


def _is_reviewable(excel_row, f: dict) -> bool:
    # Must match the frontend's isReviewable (reportHtml.ts).
    return excel_row != 0 and f.get("type") not in ("register_summary", "system")


def tone_key(lang: str) -> str:
    return f"tone|{lang}"


def numbered_findings(lang: str, results: dict):
    """Yields (number, key, row, finding) for every reviewable finding of this
    language — the same order and numbers the manager's report page uses
    (reportHtml.ts), so both sides can refer to «замечание №5». The
    «Тон обращения» summary, when the language has one, is always №1 (row is
    None for it); the real findings follow, numbered across sheets."""
    n = 0
    tone = _tone_finding(lang, results)
    if tone is not None:
        n = 1
        yield 1, tone_key(lang), None, tone
    for sheet_idx, sheet in enumerate((results or {}).get("sheets", [])):
        if lang not in (sheet.get("languages_checked") or []):
            continue
        for row in (sheet.get("languages") or {}).get(lang) or []:
            for fi, f in enumerate(row.get("findings") or []):
                if not _is_reviewable(row.get("excel_row"), f):
                    continue
                n += 1
                yield n, f"{sheet_idx}|{lang}|{row.get('excel_row')}|{fi}", row, f


def pending_keys(lang: str, results: dict, review: dict) -> set:
    """Findings waiting for the head of QA: marked ✓ or ? by the manager and
    not yet sent on to the translator."""
    review = review or {}
    out = set()
    for _, key, _, _ in numbered_findings(lang, results):
        e = review.get(key) or {}
        if e.get("decision") in ("accept", "question") and not e.get("sent") and not e.get("okk_removed"):
            out.add(key)
    return out


def sent_keys(lang: str, results: dict, review: dict) -> set:
    """Findings the head of QA left for the translator."""
    review = review or {}
    out = set()
    for _, key, _, _ in numbered_findings(lang, results):
        e = review.get(key) or {}
        if e.get("decision") in ("accept", "question") and e.get("sent") and not e.get("okk_removed"):
            out.add(key)
    return out


def _tone_finding(lang: str, results: dict):
    for sheet in (results or {}).get("sheets", []):
        if lang not in (sheet.get("languages_checked") or []):
            continue
        for row in (sheet.get("languages") or {}).get(lang) or []:
            if row.get("excel_row") == 0:
                for f in row.get("findings") or []:
                    if f.get("type") == "register_summary":
                        return f
    return None


_SCRIPT = """
(function () {
  var base = location.pathname.replace(/\\/+$/, "");
  var timers = {};
  function post(path, body, item, after) {
    var st = item.querySelector(".status");
    if (st) st.textContent = "Сохраняю…";
    return fetch(base + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) {
      if (!r.ok) throw new Error();
      if (st) {
        st.textContent = "Сохранено";
        setTimeout(function () { if (st.textContent === "Сохранено") st.textContent = ""; }, 1500);
      }
      if (after) after();
    }).catch(function () { if (st) st.textContent = "⚠ Не сохранилось — проверьте интернет"; });
  }
  function key(item) { return item.getAttribute("data-key"); }
  function sendOkk(item, action) {
    var c = item.querySelector(".okk-comment");
    var l = item.querySelector(".okk-links");
    post("/okk", { key: key(item), action: action || null, comment: c ? c.value : "", links: l ? l.value : null }, item, function () {
      if (action === "remove") item.parentNode.removeChild(item);
      if (action === "keep") location.reload();
    });
  }
  function sendAnswer(item) {
    var d = item.classList.contains("done") ? "done" : item.classList.contains("na") ? "na" : null;
    post("/respond", { key: key(item), decision: d, comment: item.querySelector(".tr-comment").value }, item);
  }
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest && ev.target.closest(".btn");
    if (!b) return;
    var item = b.closest(".item");
    if (b.classList.contains("btn-keep")) { sendOkk(item, "keep"); return; }
    if (b.classList.contains("btn-remove")) { sendOkk(item, "remove"); return; }
    var cls = b.classList.contains("btn-done") ? "done" : "na";
    var on = !item.classList.contains(cls);
    item.classList.remove("done", "na");
    if (on) item.classList.add(cls);
    sendAnswer(item);
  });
  document.addEventListener("change", function (ev) {
    var t = ev.target;
    if (!t.classList || !t.classList.contains("checked-box")) return;
    var item = t.closest(".item");
    post("/checked", { key: key(item), checked: t.checked }, item);
  });
  document.addEventListener("input", function (ev) {
    var t = ev.target;
    if (!t.classList) return;
    var item = t.closest(".item");
    if (!item) return;
    var k = key(item);
    if (t.classList.contains("tr-comment")) {
      clearTimeout(timers[k]);
      timers[k] = setTimeout(function () { sendAnswer(item); }, 800);
    } else if (t.classList.contains("okk-comment") || t.classList.contains("okk-links")) {
      clearTimeout(timers[k]);
      timers[k] = setTimeout(function () { sendOkk(item, null); }, 800);
    }
  });
})();
"""


def render_shared_report(
    filename: str, lang: str, results: dict, review: dict,
    translator_review: dict | None = None, nonce: str = "",
) -> str:
    review = review or {}
    translator_review = translator_review or {}
    items_html = []
    pending_count = 0
    for num, key, row, f in numbered_findings(lang, results):
        entry = review.get(key) or {}
        decision = entry.get("decision")
        if decision not in ("accept", "question"):
            continue
        if entry.get("okk_removed"):
            continue
        pending = not entry.get("sent")
        tr = translator_review.get(key) or {}
        tr_decision = {"accept": "done", "reject": "na"}.get(tr.get("decision"), tr.get("decision"))
        classes = ["item"]
        if row is None:
            classes.append("tone")
        if pending:
            pending_count += 1
            classes += ["pending", "p-question" if decision == "question" else "p-accept"]
        elif tr_decision in ("done", "na"):
            classes.append(tr_decision)

        if row is None:
            body_html = f'<div class="field"><span class="label">Тон обращения:</span> {_t(_tone_text(f))}</div>'
            platform_html = ""
        else:
            body_html = (
                f'<div class="field"><span class="label">Источник:</span> {_t(row.get("source"))}</div>'
                f'<div class="field"><span class="label">Перевод:</span> {_t(row.get("translation"))}</div>'
            )
            platform_html = f'<div class="comment"><span class="label">Комментарий платформы:</span> {_t(_strip_rows(f.get("message")))}</div>'
        links_html = _links_html(entry.get("links", ""))
        if pending:
            # The QA head can add or fix the Crowdin link(s) at this stage.
            raw_links = "\n".join(p for p in re.split(r"\s+", entry.get("links") or "") if p)
            links_html = (
                '<label class="label block-label">Ссылки на Crowdin:</label>'
                f'<textarea class="okk-links" placeholder="https://crowdin.com/… — каждая ссылка с новой строки">{_e(raw_links)}</textarea>'
            )
        okk_comment = (entry.get("okk_comment") or "").strip()

        if pending:
            # Stage 1 — head of QA. The manager's note is for the QA head only.
            note = (entry.get("note") or "").strip()
            note_html = (
                f'<div class="field"><span class="label">Примечание менеджера:</span> <span class="red">{_e(note)}</span></div>'
            ) if note else ""
            tail = (
                '<label class="label block-label">Комментарий для переводчика:</label>'
                f'<textarea class="okk-comment" placeholder="Вопрос или уточнение для переводчика (необязательно)">{_e(okk_comment)}</textarea>'
                '<div class="actions"><span class="status"></span>'
                '<button type="button" class="btn btn-keep">Оставить переводчику</button>'
                '<button type="button" class="btn btn-remove">Убрать</button></div>'
            )
            check_html = ""
        else:
            # Stage 2 — translator.
            note_html = ""
            okk_html = (
                f'<div class="field"><span class="label">Комментарий для переводчика:</span> <span class="red">{_e(okk_comment)}</span></div>'
            ) if okk_comment else ""
            platform_html += okk_html
            tail = (
                '<label class="label block-label">Примечание переводчика:</label>'
                f'<textarea class="tr-comment" placeholder="Комментарий переводчика (опционально)">{_e(tr.get("comment"))}</textarea>'
                '<div class="actions"><span class="status"></span>'
                '<button type="button" class="btn btn-done">Правка внесена</button>'
                '<button type="button" class="btn btn-na">Не актуально</button></div>'
            )
            checked = " checked" if tr.get("checked") else ""
            check_html = (
                f'<label class="check" title="Для менеджера: правка проверена"><input type="checkbox" class="checked-box"{checked} /> Проверено</label>'
            )
        items_html.append(
            f'<div class="{" ".join(classes)}" data-key="{_e(key)}">'
            f"{check_html}"
            f'<div class="num">№{num}</div>'
            f"{body_html}{note_html}{links_html}{platform_html}{tail}"
            "</div>"
        )

    body = (
        f"<h1>Замечания по переводу — {_e(lang.upper())}</h1>"
        f'<div class="muted">{_e(filename)}</div>'
    )
    if items_html:
        intro = f"Замечаний: {len(items_html)}."
        if pending_count:
            intro += f" Ожидают проверки руководителя ОКК: {pending_count}."
        intro += " Всё сохраняется автоматически."
        body += f'<p class="muted">{intro}</p>' + "".join(items_html)
        return _page(f"Замечания — {lang.upper()}", body, _SCRIPT, nonce)
    body += '<div class="empty">Замечаний к исправлению нет.</div>'
    return _page(f"Замечания — {lang.upper()}", body)
