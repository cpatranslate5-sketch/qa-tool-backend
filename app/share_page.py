"""Public report page for translators (2026-09-29, redesigned 2026-09-30 —
Александр).

Opened by a share link (see models.ShareLink): shows ONE language of ONE
multi-check report — only the findings the manager accepted (✓). Each one
shows its number (the same number the manager sees in his report), source,
translation, the manager's note, clickable Crowdin link(s), the platform's
comment, a field for the translator's own comment, and «Принять» /
«Отклонить» buttons in the bottom-right corner (the block turns light green
/ light red). Answers are saved via POST /share/<token>/respond.

No confidence percents, costs, model names, folder or project names, and no
way to navigate anywhere else: the only links are the manager's own Crowdin
links, and the one inline script (allowed by a per-request nonce) can talk
only to this same server. Rendered live on every request, so later edits by
the manager show up for everyone who has the link.
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
  .label, .num { font-weight: 700; color: #1d4ed8; }
  .tag { color: #c026d3; font-weight: 600; }
  .general { background: #eef2ff; border: 1px solid #c7d2fe; border-radius: 10px; padding: 10px 14px; margin: 14px 0; }
  .general .row { margin-top: 4px; white-space: pre-wrap; }
  .item { background: #fff; border: 1px solid #dde1e7; border-radius: 10px; padding: 12px 14px; margin-top: 12px;
          transition: background .15s, border-color .15s; }
  .item.tone { background: #eef2ff; border-color: #c7d2fe; }
  .item.question { background: #fff8db; border-color: #f0d98c; }
  .item.accepted { background: #eaf7ef; border-color: #9fd5b3; }
  .item.rejected { background: #fdeeee; border-color: #f0b4b4; }
  .num { font-size: 0.95rem; margin-bottom: 6px; }
  .mgr-note-edit { display: block; width: 100%; min-height: 48px; margin-top: 4px; font: inherit; font-size: 0.88rem;
                   padding: 6px 8px; border: 1px solid #e3c96a; border-radius: 6px; resize: vertical; background: #fff; }
  .item .btn-keep, .item .btn-remove, .item.question .btn-accept, .item.question .btn-reject, .item.question .answer { display: none; }
  .item.question .btn-keep, .item.question .btn-remove { display: inline-block; }
  .btn-keep { color: #17703c; border-color: #17703c; }
  .btn-remove { color: #b42318; border-color: #b42318; }
  .field { font-size: 0.9rem; margin-top: 4px; }
  .mgr-note { white-space: pre-wrap; }
  .links a { color: #4f46e5; word-break: break-all; display: block; }
  .comment { border-left: 3px solid #d98a1f; background: rgba(0,0,0,0.03); padding: 6px 10px; margin-top: 8px; font-size: 0.9rem; }
  .tr-label { display: block; font-size: 0.85rem; margin-top: 10px; }
  .tr-comment { width: 100%; min-height: 54px; margin-top: 4px; font: inherit; font-size: 0.88rem; padding: 6px 8px;
                border: 1px solid #dde1e7; border-radius: 6px; resize: vertical; background: #fff; }
  .actions { display: flex; justify-content: flex-end; align-items: center; gap: 8px; margin-top: 10px; }
  .status { font-size: 0.8rem; color: #6b7280; margin-right: auto; }
  .btn { border: 1px solid; border-radius: 8px; padding: 6px 16px; font: inherit; font-size: 0.88rem; cursor: pointer; background: #fff; }
  .btn-accept { color: #17703c; border-color: #17703c; }
  .btn-reject { color: #b42318; border-color: #b42318; }
  .item.accepted .btn-accept { background: #17703c; color: #fff; }
  .item.rejected .btn-reject { background: #b42318; color: #fff; }
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


def keys_with_decision(lang: str, results: dict, review: dict, decision: str) -> set:
    review = review or {}
    return {
        key for _, key, _, _ in numbered_findings(lang, results)
        if (review.get(key) or {}).get("decision") == decision
    }


def accepted_keys(lang: str, results: dict, review: dict) -> set:
    return keys_with_decision(lang, results, review, "accept")


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
  function post(path, body, st, after) {
    st.textContent = "Сохраняю…";
    return fetch(base + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) {
      if (!r.ok) throw new Error();
      st.textContent = "Сохранено";
      setTimeout(function () { if (st.textContent === "Сохранено") st.textContent = ""; }, 1500);
      if (after) after();
    }).catch(function () { st.textContent = "⚠ Не сохранилось — проверьте интернет"; });
  }
  function sendAnswer(item) {
    var decision = item.classList.contains("accepted") ? "accept" : item.classList.contains("rejected") ? "reject" : null;
    post("/respond", { key: item.getAttribute("data-key"), decision: decision, comment: item.querySelector(".tr-comment").value },
         item.querySelector(".status"));
  }
  function sendQuestion(item, action) {
    var noteEl = item.querySelector(".mgr-note-edit");
    return post("/question", { key: item.getAttribute("data-key"), action: action || null, note: noteEl ? noteEl.value : "" },
         item.querySelector(".status"), function () {
      if (action === "remove") { item.parentNode.removeChild(item); }
      if (action === "keep") {
        item.classList.remove("question");
        var note = noteEl.value.trim();
        var holder = item.querySelector(".mgr-note-field");
        if (note) {
          holder.innerHTML = '<span class="label">Примечание менеджера:</span> <span class="mgr-note"></span>';
          holder.querySelector(".mgr-note").textContent = note;
        } else {
          holder.parentNode.removeChild(holder);
        }
      }
    });
  }
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest && ev.target.closest(".btn");
    if (!b) return;
    var item = b.closest(".item");
    if (b.classList.contains("btn-keep")) { sendQuestion(item, "keep"); return; }
    if (b.classList.contains("btn-remove")) { sendQuestion(item, "remove"); return; }
    var cls = b.classList.contains("btn-accept") ? "accepted" : "rejected";
    var on = !item.classList.contains(cls);
    item.classList.remove("accepted", "rejected");
    if (on) item.classList.add(cls);
    sendAnswer(item);
  });
  document.addEventListener("input", function (ev) {
    var t = ev.target;
    if (!t.classList) return;
    var item = t.closest(".item");
    if (!item) return;
    var k = item.getAttribute("data-key");
    if (t.classList.contains("tr-comment")) {
      clearTimeout(timers[k]);
      timers[k] = setTimeout(function () { sendAnswer(item); }, 800);
    } else if (t.classList.contains("mgr-note-edit")) {
      clearTimeout(timers[k]);
      timers[k] = setTimeout(function () { sendQuestion(item, null); }, 800);
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
    for num, key, row, f in numbered_findings(lang, results):
        entry = review.get(key) or {}
        decision = entry.get("decision")
        if decision not in ("accept", "question"):
            continue
        is_question = decision == "question"
        tr = translator_review.get(key) or {}
        classes = ["item"]
        if row is None:
            classes.append("tone")
        if is_question:
            classes.append("question")
        else:
            classes.append({"accept": "accepted", "reject": "rejected"}.get(tr.get("decision"), ""))
        note = (entry.get("note") or "").strip()
        if is_question:
            # Before «Оставить переводчику»/«Убрать» the manager's note can
            # still be edited right here (whoever opens the link first).
            note_html = (
                '<div class="field mgr-note-field"><span class="label">Примечание менеджера:</span>'
                f'<textarea class="mgr-note-edit" placeholder="Можно дописать перед решением">{_e(note)}</textarea></div>'
            )
        elif note:
            note_html = (
                '<div class="field mgr-note-field"><span class="label">Примечание менеджера:</span> '
                f'<span class="mgr-note">{_e(note)}</span></div>'
            )
        else:
            note_html = ""
        if row is None:
            body_html = f'<div class="field"><span class="label">Тон обращения:</span> {_e(f.get("message"))}</div>'
            platform_html = ""
        else:
            body_html = (
                f'<div class="field"><span class="label">Источник:</span> {_t(row.get("source"))}</div>'
                f'<div class="field"><span class="label">Перевод:</span> {_t(row.get("translation"))}</div>'
            )
            platform_html = f'<div class="comment"><span class="label">Комментарий платформы:</span> {_t(f.get("message"))}</div>'
        items_html.append(
            f'<div class="{" ".join(c for c in classes if c)}" data-key="{_e(key)}">'
            f'<div class="num">№{num}</div>'
            f"{body_html}{note_html}{_links_html(entry.get('links', ''))}{platform_html}"
            '<div class="answer">'
            '<label class="label tr-label">Примечание переводчика:</label>'
            f'<textarea class="tr-comment" placeholder="Ваш комментарий (необязательно)">{_e(tr.get("comment"))}</textarea>'
            "</div>"
            '<div class="actions"><span class="status"></span>'
            '<button type="button" class="btn btn-keep">Оставить переводчику</button>'
            '<button type="button" class="btn btn-remove">Убрать</button>'
            '<button type="button" class="btn btn-accept">✓ Принять</button>'
            '<button type="button" class="btn btn-reject">✕ Отклонить</button></div>'
            "</div>"
        )

    # A language without a tone summary can still have a general note and
    # links from the manager (review key "note|<lang>").
    general = review.get(f"note|{lang}") or {}
    g_note = (general.get("note") or "").strip()
    g_links = _links_html(general.get("links") or "")
    general_html = ""
    if g_note or g_links:
        general_html = (
            '<div class="general">'
            + (f'<div class="row"><span class="label">Примечание менеджера:</span> {_e(g_note)}</div>' if g_note else "")
            + g_links
            + "</div>"
        )

    body = (
        f"<h1>Замечания по переводу — {_e(lang.upper())}</h1>"
        f'<div class="muted">{_e(filename)}</div>'
        + general_html
    )
    if items_html:
        body += (
            f'<p class="muted">Замечаний: {len(items_html)}. По каждому нажмите «Принять» или «Отклонить» '
            "и при необходимости оставьте комментарий — всё сохраняется автоматически.</p>"
            + "".join(items_html)
        )
        return _page(f"Замечания — {lang.upper()}", body, _SCRIPT, nonce)
    body += '<div class="empty">Замечаний к исправлению нет.</div>'
    return _page(f"Замечания — {lang.upper()}", body)
