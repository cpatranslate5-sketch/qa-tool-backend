"""Public, read-only report page for translators (2026-09-29, Александр).

Opened by a share link (see models.ShareLink): shows ONE language of ONE
multi-check report — only the findings the manager accepted (✓), with the
platform's comment, the manager's note and clickable Crowdin link(s). No
confidence percents, costs, model names, folder or project names, and no way
to navigate anywhere else: the page has no scripts and no links except the
manager's own Crowdin links. Rendered live on every request, so later edits
to the review are visible to everyone who has the link.
"""
import html
import re

_URL_RE = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)

# Response headers that lock the page down: nothing can load or run, it can't
# be framed, search engines won't index it, and opening a Crowdin link won't
# leak this page's address to the other site.
SHARE_PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
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
  .note { background: #eef2ff; border: 1px solid #c7d2fe; border-radius: 10px; padding: 10px 14px; margin: 14px 0; white-space: pre-wrap; }
  .item { background: #fff; border: 1px solid #dde1e7; border-radius: 10px; padding: 12px 14px; margin-top: 12px; }
  .item-head { font-weight: 600; font-size: 0.9rem; margin-bottom: 6px; }
  .pair { font-size: 0.88rem; color: #444b58; display: flex; flex-direction: column; gap: 2px; margin-bottom: 8px; }
  .label { font-weight: 600; }
  .comment { border-left: 3px solid #d98a1f; background: #fafafa; padding: 6px 10px; font-size: 0.9rem; }
  .mgr-note { border-left: 3px solid #6366f1; background: #f5f6ff; padding: 6px 10px; margin-top: 6px; font-size: 0.9rem; white-space: pre-wrap; }
  .links { margin-top: 8px; font-size: 0.88rem; display: flex; flex-direction: column; gap: 2px; }
  .links a { color: #4f46e5; word-break: break-all; }
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
    return '<div class="links"><span class="label">Ссылки:</span>' + "".join(items) + "</div>"


def _page(title: str, body: str) -> str:
    return (
        '<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />'
        '<meta name="robots" content="noindex, nofollow" />'
        f"<title>{_e(title)}</title><style>{_CSS}</style></head>"
        f'<body><div class="page">{body}</div></body></html>'
    )


def render_not_found() -> str:
    return _page("Ссылка недействительна", "<h1>Ссылка недействительна</h1>"
                 '<p class="muted">Эта ссылка отключена или не существует. Обратитесь к менеджеру.</p>')


def render_shared_report(filename: str, lang: str, results: dict, review: dict) -> str:
    review = review or {}
    tone = ""
    items_html = []
    for sheet_idx, sheet in enumerate((results or {}).get("sheets", [])):
        rows = (sheet.get("languages") or {}).get(lang)
        if not rows:
            continue
        for row in rows:
            findings = row.get("findings") or []
            if row.get("excel_row") == 0:
                for f in findings:
                    if f.get("type") == "register_summary" and not tone:
                        tone = f.get("message", "")
                continue
            for fi, f in enumerate(findings):
                key = f"{sheet_idx}|{lang}|{row.get('excel_row')}|{fi}"
                entry = review.get(key) or {}
                if entry.get("decision") != "accept":
                    continue
                note = (entry.get("note") or "").strip()
                note_html = (
                    '<div class="mgr-note"><span class="label">Примечание:</span> ' + _e(note) + "</div>"
                ) if note else ""
                context_html = (" — " + _e(row.get("context"))) if row.get("context") else ""
                items_html.append(
                    '<div class="item">'
                    f'<div class="item-head">Строка {_e(row.get("excel_row"))}{context_html}</div>'
                    '<div class="pair">'
                    f'<div><span class="label">Источник:</span> {_e(row.get("source"))}</div>'
                    f'<div><span class="label">Перевод:</span> {_e(row.get("translation"))}</div>'
                    "</div>"
                    f'<div class="comment">{_e(f.get("message"))}</div>'
                    f"{note_html}"
                    f"{_links_html(entry.get('links', ''))}"
                    "</div>"
                )
    general = ((review.get(f"note|{lang}") or {}).get("note") or "").strip()
    head = (
        f"<h1>Замечания по переводу — {_e(lang.upper())}</h1>"
        f'<div class="muted">{_e(filename)}</div>'
        + (f'<div class="muted">{_e(tone)}</div>' if tone else "")
    )
    body = head
    if general:
        body += f'<div class="note"><span class="label">Примечание менеджера:</span>\n{_e(general)}</div>'
    if items_html:
        body += f'<p class="muted">Замечаний к исправлению: {len(items_html)}.</p>' + "".join(items_html)
    else:
        body += '<div class="empty">Замечаний к исправлению нет.</div>'
    return _page(f"Замечания — {lang.upper()}", body)
