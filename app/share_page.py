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
  .label { font-weight: 600; }
  .general { background: #eef2ff; border: 1px solid #c7d2fe; border-radius: 10px; padding: 10px 14px; margin: 14px 0; }
  .general .row { margin-top: 4px; white-space: pre-wrap; }
  .item { background: #fff; border: 1px solid #dde1e7; border-radius: 10px; padding: 12px 14px; margin-top: 12px;
          transition: background .15s, border-color .15s; }
  .item.accepted { background: #eaf7ef; border-color: #9fd5b3; }
  .item.rejected { background: #fdeeee; border-color: #f0b4b4; }
  .num { font-weight: 700; font-size: 0.95rem; margin-bottom: 6px; }
  .field { font-size: 0.9rem; margin-top: 4px; }
  .mgr-note { white-space: pre-wrap; }
  .links a { color: #4f46e5; word-break: break-all; display: block; }
  .comment { border-left: 3px solid #d98a1f; background: rgba(0,0,0,0.03); padding: 6px 10px; margin-top: 8px; font-size: 0.9rem; }
  .tr-label { display: block; font-weight: 600; font-size: 0.85rem; margin-top: 10px; }
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


def numbered_findings(lang: str, results: dict):
    """Yields (number, key, row, finding) for every reviewable finding of this
    language, numbered 1..N across sheets — the same order and numbers the
    manager's report page uses (reportHtml.ts), so both sides can refer to
    «замечание №5»."""
    n = 0
    for sheet_idx, sheet in enumerate((results or {}).get("sheets", [])):
        if lang not in (sheet.get("languages_checked") or []):
            continue
        for row in (sheet.get("languages") or {}).get(lang) or []:
            for fi, f in enumerate(row.get("findings") or []):
                if not _is_reviewable(row.get("excel_row"), f):
                    continue
                n += 1
                yield n, f"{sheet_idx}|{lang}|{row.get('excel_row')}|{fi}", row, f


def accepted_keys(lang: str, results: dict, review: dict) -> set:
    review = review or {}
    return {
        key for _, key, _, _ in numbered_findings(lang, results)
        if (review.get(key) or {}).get("decision") == "accept"
    }


def _tone_message(lang: str, results: dict) -> str:
    for sheet in (results or {}).get("sheets", []):
        for row in (sheet.get("languages") or {}).get(lang) or []:
            if row.get("excel_row") == 0:
                for f in row.get("findings") or []:
                    if f.get("type") == "register_summary":
                        return f.get("message", "")
    return ""


_SCRIPT = """
(function () {
  var base = location.pathname.replace(/\\/+$/, "") + "/respond";
  var timers = {};
  function send(item) {
    var st = item.querySelector(".status");
    var decision = item.classList.contains("accepted") ? "accept" : item.classList.contains("rejected") ? "reject" : null;
    st.textContent = "Сохраняю…";
    fetch(base, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: item.getAttribute("data-key"), decision: decision, comment: item.querySelector(".tr-comment").value })
    }).then(function (r) {
      if (!r.ok) throw new Error();
      st.textContent = "Сохранено";
      setTimeout(function () { if (st.textContent === "Сохранено") st.textContent = ""; }, 1500);
    }).catch(function () { st.textContent = "⚠ Не сохранилось — проверьте интернет"; });
  }
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest && ev.target.closest(".btn");
    if (!b) return;
    var item = b.closest(".item");
    var cls = b.classList.contains("btn-accept") ? "accepted" : "rejected";
    var on = !item.classList.contains(cls);
    item.classList.remove("accepted", "rejected");
    if (on) item.classList.add(cls);
    send(item);
  });
  document.addEventListener("input", function (ev) {
    if (!ev.target.classList || !ev.target.classList.contains("tr-comment")) return;
    var item = ev.target.closest(".item");
    var k = item.getAttribute("data-key");
    clearTimeout(timers[k]);
    timers[k] = setTimeout(function () { send(item); }, 800);
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
        if entry.get("decision") != "accept":
            continue
        tr = translator_review.get(key) or {}
        state = {"accept": " accepted", "reject": " rejected"}.get(tr.get("decision"), "")
        note = (entry.get("note") or "").strip()
        note_html = (
            '<div class="field"><span class="label">Примечание менеджера:</span> '
            f'<span class="mgr-note">{_e(note)}</span></div>'
        ) if note else ""
        items_html.append(
            f'<div class="item{state}" data-key="{_e(key)}">'
            f'<div class="num">№{num}</div>'
            f'<div class="field"><span class="label">Источник:</span> {_e(row.get("source"))}</div>'
            f'<div class="field"><span class="label">Перевод:</span> {_e(row.get("translation"))}</div>'
            f"{note_html}"
            f"{_links_html(entry.get('links', ''))}"
            f'<div class="comment"><span class="label">Комментарий платформы:</span> {_e(f.get("message"))}</div>'
            '<label class="tr-label">Примечание переводчика:</label>'
            f'<textarea class="tr-comment" placeholder="Ваш комментарий (необязательно)">{_e(tr.get("comment"))}</textarea>'
            '<div class="actions"><span class="status"></span>'
            '<button type="button" class="btn btn-accept">✓ Принять</button>'
            '<button type="button" class="btn btn-reject">✕ Отклонить</button></div>'
            "</div>"
        )

    # Tone-of-address box: the check's tone summary plus the manager's
    # general note and links for this language (review key "note|<lang>").
    tone = _tone_message(lang, results)
    general = review.get(f"note|{lang}") or {}
    g_note = (general.get("note") or "").strip()
    g_links = _links_html(general.get("links") or "")
    general_html = ""
    if tone or g_note or g_links:
        general_html = (
            '<div class="general">'
            + (f'<div><span class="label">Тон обращения:</span> {_e(tone)}</div>' if tone else "")
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
