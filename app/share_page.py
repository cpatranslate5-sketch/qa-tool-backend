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
  .links { margin-top: 10px; }
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
  [hidden] { display: none !important; }
  .lang-bar { display: flex; flex-wrap: wrap; gap: 6px; margin: 14px 0 4px; position: sticky; top: 0; background: #f5f6f8; padding: 8px 0; z-index: 5; }
  .lang-btn { background: #fff; border: 1px solid #dde1e7; border-radius: 999px; font: inherit; font-size: 0.85rem; padding: 5px 12px; cursor: pointer; }
  .lang-btn.active { background: #3949ab; border-color: #3949ab; color: #fff; font-weight: 600; }
  .copy-link { margin-left: auto; background: #fff; border: 1px solid #c7d2fe; color: #3949ab; border-radius: 8px; font: inherit; font-size: 0.82rem; padding: 5px 10px; cursor: pointer; }
  .lang-section { margin-top: 22px; }
  .lang-title { font-size: 1.15rem; margin: 0 0 4px; padding-bottom: 6px; border-bottom: 2px solid #c7d2fe; }
  .corner { position: absolute; top: 10px; right: 12px; display: flex; align-items: center; gap: 6px; }
  .num { padding-right: 150px; }
  .conf { font-weight: 600; color: #6b7280; font-size: 0.85rem; }
  .undo-bar { display: flex; align-items: center; gap: 10px; margin-top: 10px; padding: 8px 12px; border-radius: 8px;
              background: #1c2230; color: #fff; font-size: 0.88rem; }
  .undo-bar button { margin-left: auto; background: #fff; color: #1c2230; border: none; border-radius: 6px; padding: 5px 12px;
                     font: inherit; font-size: 0.85rem; font-weight: 600; cursor: pointer; }
  .item.undoable .actions, .item.undoable textarea, .item.undoable .block-label { display: none; }
  .item.undo-keep { background: #eaf7ef; border-color: #17703c; }
  .item.undo-remove { background: #fdeeee; border-color: #b42318; opacity: 0.85; }
  .item.kept .actions, .item.kept textarea.okk-comment, .item.kept textarea.okk-links, .item.kept .block-label { display: none; }
  .kept-note { margin-top: 10px; color: #17703c; font-weight: 600; font-size: 0.88rem; }
  .save-btn { background: #fff; border: 1px solid #dde1e7; border-radius: 6px; width: 30px; height: 26px; cursor: pointer;
              font-size: 0.9rem; line-height: 1; padding: 0; }
  .save-btn:hover { border-color: #6366f1; }
  .save-btn.saved { cursor: default; }
  .save-login { background: #fff; border: 1px solid #c7d2fe; border-radius: 10px; padding: 10px 14px; margin: 12px 0;
                display: flex; flex-wrap: wrap; gap: 8px; align-items: center; font-size: 0.88rem; }
  .save-login input { font: inherit; font-size: 0.88rem; padding: 5px 8px; border: 1px solid #dde1e7; border-radius: 6px; }
  .save-login .msg { color: #dc2626; }
  .save-who { font-size: 0.8rem; color: #6b7280; margin-top: 4px; }
  .save-who a { color: #4f46e5; cursor: pointer; }
  .check { display: none; align-items: center; gap: 6px;
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
  function okkBody(item, action) {
    var c = item.querySelector(".okk-comment");
    var l = item.querySelector(".okk-links");
    return { key: key(item), action: action || null, comment: c ? c.value : "", links: l ? l.value : null };
  }
  function sendOkk(item, action) {
    post("/okk", okkBody(item, action), item, function () {
      if (action === "remove") item.parentNode.removeChild(item);
      if (action === "keep") {
        item.classList.add("kept");
        var n = document.createElement("div");
        n.className = "kept-note";
        n.textContent = "✓ Оставлено переводчику. Обновите страницу, чтобы увидеть, как блок выглядит у переводчика.";
        item.appendChild(n);
      }
    });
  }
  // «Оставить переводчику» / «Убрать» can be undone for 10 seconds; acting on
  // the next finding ends that window early (2026-10-01, Александр).
  var undo = null;
  function clearUndo(u) {
    clearInterval(u.tick);
    if (u.bar && u.bar.parentNode) u.bar.parentNode.removeChild(u.bar);
    u.item.classList.remove("undoable", "undo-keep", "undo-remove");
  }
  function commitUndo() {
    if (!undo) return;
    var u = undo; undo = null;
    clearUndo(u);
    if (u.action === "remove") u.item.style.display = "none";
    sendOkk(u.item, u.action);
  }
  function startUndo(item, action) {
    if (undo && undo.item === item) return;
    commitUndo();
    clearTimeout(timers[key(item)]);
    var u = { item: item, action: action, left: 10 };
    item.classList.add("undoable", action === "keep" ? "undo-keep" : "undo-remove");
    var bar = document.createElement("div");
    bar.className = "undo-bar";
    var label = document.createElement("span");
    var btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "Отменить";
    bar.appendChild(label); bar.appendChild(btn);
    item.appendChild(bar);
    u.bar = bar;
    function render() {
      label.textContent = (action === "keep" ? "Оставлено переводчику." : "Убрано из отчёта.") + " Отменить можно ещё " + u.left + " с.";
    }
    render();
    btn.addEventListener("click", function () { if (undo === u) { undo = null; clearUndo(u); } });
    u.tick = setInterval(function () {
      u.left -= 1;
      if (u.left <= 0) { if (undo === u) commitUndo(); } else render();
    }, 1000);
    undo = u;
  }
  // Closing the page inside the 10 seconds still saves the decision.
  window.addEventListener("pagehide", function () {
    if (!undo) return;
    var u = undo; undo = null;
    try {
      navigator.sendBeacon(base + "/okk", new Blob([JSON.stringify(okkBody(u.item, u.action))], { type: "application/json" }));
    } catch (e) {}
  });
  function sendAnswer(item) {
    var d = item.classList.contains("done") ? "done" : item.classList.contains("na") ? "na" : null;
    post("/respond", { key: key(item), decision: d, comment: item.querySelector(".tr-comment").value }, item);
  }
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest && ev.target.closest(".btn");
    if (!b) return;
    var item = b.closest(".item");
    if (!item) return;
    if (b.classList.contains("btn-keep")) { startUndo(item, "keep"); return; }
    if (b.classList.contains("btn-remove")) { startUndo(item, "remove"); return; }
    var cls = b.classList.contains("btn-done") ? "done" : "na";
    var on = !item.classList.contains(cls);
    item.classList.remove("done", "na");
    if (on) item.classList.add(cls);
    sendAnswer(item);
  });
  // 💾 → «Сохранённое» of the viewer's own folder.
  var LS = "qa-share-save-folder";
  var pendingSave = null;
  function creds() { try { return JSON.parse(localStorage.getItem(LS) || "null"); } catch (e) { return null; } }
  function setCreds(c) { try { if (c) localStorage.setItem(LS, JSON.stringify(c)); else localStorage.removeItem(LS); } catch (e) {} }
  function showWho() {
    var w = document.getElementById("save-who"); if (!w) return;
    var c = creds();
    if (!c) { w.hidden = true; return; }
    w.hidden = false;
    w.textContent = "Сохранение в папку «" + c.folder + "». ";
    var a = document.createElement("a"); a.textContent = "Сменить папку";
    a.addEventListener("click", function () { setCreds(null); showWho(); });
    w.appendChild(a);
  }
  function doSave(item, c, onFail) {
    var b = item.querySelector(".save-btn");
    b.disabled = true;
    fetch(base + "/save-case", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: key(item), folder: c.folder, code: c.code })
    }).then(function (r) {
      if (r.status === 401 || r.status === 404) { b.disabled = false; setCreds(null); showWho(); if (onFail) onFail("Неверная папка или пароль."); return; }
      if (!r.ok) throw new Error();
      setCreds(c); showWho();
      document.getElementById("save-login").hidden = true;
      b.classList.add("saved"); b.textContent = "✅"; b.title = "Сохранено в папку «" + c.folder + "»";
    }).catch(function () { b.disabled = false; if (onFail) onFail("Не удалось сохранить — проверьте интернет."); });
  }
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest && ev.target.closest(".save-btn");
    if (!b || b.classList.contains("saved") || b.disabled) return;
    var item = b.closest(".item");
    var c = creds();
    if (c) { doSave(item, c, function (m) { pendingSave = item; var p = document.getElementById("save-login"); p.hidden = false; document.getElementById("save-msg").textContent = m; }); return; }
    pendingSave = item;
    var p = document.getElementById("save-login");
    p.hidden = false; document.getElementById("save-msg").textContent = "";
    p.scrollIntoView({ block: "center" });
    document.getElementById("save-folder").focus();
  });
  var go = document.getElementById("save-go");
  if (go) {
    go.addEventListener("click", function () {
      var c = { folder: document.getElementById("save-folder").value.trim(), code: document.getElementById("save-code").value };
      if (!c.folder || !c.code || !pendingSave) return;
      doSave(pendingSave, c, function (m) { document.getElementById("save-msg").textContent = m; });
    });
    document.getElementById("save-cancel").addEventListener("click", function () {
      document.getElementById("save-login").hidden = true; pendingSave = null;
    });
    showWho();
  }
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


ALL_LANGS = "*"  # a share link for every language of the report (2026-10-01)

_FLAG_FALLBACK = {
    "en": "🇬🇧", "ru": "🇷🇺", "es": "🇪🇸", "fr": "🇫🇷", "de": "🇩🇪", "it": "🇮🇹", "ar": "🇸🇦", "zh": "🇨🇳",
    "ja": "🇯🇵", "ko": "🇰🇷", "vi": "🇻🇳", "th": "🇹🇭", "pl": "🇵🇱", "tr": "🇹🇷", "uk": "🇺🇦", "id": "🇮🇩",
    "ms": "🇲🇾", "hi": "🇮🇳", "hing": "🇮🇳", "kk": "🇰🇿", "el": "🇬🇷", "sw": "🇹🇿", "te": "🇮🇳", "ur": "🇵🇰",
    "bn": "🇧🇩", "ky": "🇰🇬", "mr": "🇮🇳", "tg": "🇹🇯", "tl": "🇵🇭", "fil": "🇵🇭", "pt": "🇧🇷", "az": "🇦🇿",
    "uz": "🇺🇿", "ro": "🇷🇴",
}


def _region_flag(cc: str) -> str:
    return "".join(chr(0x1F1E6 + ord(c) - ord("a")) for c in cc.lower())


def flag_for_lang(code: str) -> str:
    """Same idea as the frontend's flagForLang (lang.ts)."""
    parts = (code or "").lower().split("-")
    for part in reversed(parts[1:]):
        if len(part) == 2 and part.isalpha():
            return _region_flag(part)
    if parts[0] in _FLAG_FALLBACK:
        return _FLAG_FALLBACK[parts[0]]
    if len(parts) == 1 and len(parts[0]) == 2 and parts[0].isalpha():
        return _region_flag(parts[0])
    return "🌐"


def report_langs(results: dict) -> list[str]:
    """Every checked language of the report, alphabetically."""
    langs = []
    for sheet in (results or {}).get("sheets", []):
        for l in sheet.get("languages_checked") or []:
            if l not in langs:
                langs.append(l)
    return sorted(langs, key=lambda x: x.lower())


def _lang_items(lang: str, results: dict, review: dict, translator_review: dict) -> tuple[list[str], int]:
    """The HTML blocks of one language (and how many wait for the QA head)."""
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
        # The AI's own confidence — for the QA head only (stage 1), never
        # for the translator (2026-10-01).
        conf = f.get("confidence", f.get("sonnet_percent"))
        conf_html = (
            f' <span class="conf" title="Уверенность ИИ в том, что это ошибка">· уверенность ИИ: {int(conf)}%</span>'
            if (not entry.get("sent")) and row is not None and isinstance(conf, (int, float)) else ""
        )
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
        # Crowdin link(s) are the last item, right above the buttons
        # (Александр, 2026-10-01).
        tail = tail.replace('<div class="actions">', links_html + '<div class="actions">', 1)
        items_html.append(
            f'<div class="{" ".join(classes)}" data-key="{_e(key)}">'
            '<div class="corner">'
            + ('<button type="button" class="save-btn" title="Сохранить в свою папку («Сохранённое»)">💾</button>' if row is not None else "")
            + f"{check_html}</div>"
            f'<div class="num">№{num}{conf_html}</div>'
            f"{body_html}{note_html}{platform_html}{tail}"
            "</div>"
        )

    return items_html, pending_count


_SAVE_LOGIN_HTML = (
    '<div id="save-login" class="save-login" hidden>'
    "<span>Сохранить в свою папку:</span>"
    '<input id="save-folder" placeholder="Название папки" autocomplete="username" />'
    '<input id="save-code" type="password" placeholder="Пароль папки" autocomplete="current-password" />'
    '<button type="button" id="save-go" class="btn btn-done">Сохранить</button>'
    '<button type="button" id="save-cancel" class="btn">Отмена</button>'
    '<span class="msg" id="save-msg"></span></div>'
    '<div id="save-who" class="save-who" hidden></div>'
)


def _intro(total: int, pending: int) -> str:
    intro = f"Замечаний: {total}."
    if pending:
        intro += f" Ожидают проверки руководителя ОКК: {pending}."
    return intro + " Всё сохраняется автоматически."


def render_shared_report(
    filename: str, lang: str, results: dict, review: dict,
    translator_review: dict | None = None, nonce: str = "",
) -> str:
    review = review or {}
    translator_review = translator_review or {}
    if lang == ALL_LANGS:
        return _render_all_langs(filename, results, review, translator_review, nonce)
    items_html, pending_count = _lang_items(lang, results, review, translator_review)
    body = (
        f"<h1>Замечания по переводу — {_e(lang.upper())}</h1>"
        f'<div class="muted">{_e(filename)}</div>'
    )
    if items_html:
        body += f'<p class="muted">{_intro(len(items_html), pending_count)}</p>'
        # 💾 on a block saves it into «Сохранённое» of the folder whose name
        # and password are entered here (asked once, remembered on this
        # device) — the page itself has no login, it's opened by a link.
        body += _SAVE_LOGIN_HTML + "".join(items_html)
        return _page(f"Замечания — {lang.upper()}", body, _SCRIPT, nonce)
    body += '<div class="empty">Замечаний к исправлению нет.</div>'
    return _page(f"Замечания — {lang.upper()}", body)


def _render_all_langs(filename: str, results: dict, review: dict, translator_review: dict, nonce: str) -> str:
    """Every language on one page (2026-10-01, Александр): a language switcher
    on top; «Все» shows each language under its own flag header in
    alphabetical order. The chosen language goes into the address (#az-az),
    so a copied link opens straight on that language."""
    sections, buttons = [], []
    total = pending_total = 0
    for l in report_langs(results):
        items, pending = _lang_items(l, results, review, translator_review)
        total += len(items)
        pending_total += pending
        label = f"{flag_for_lang(l)} {_e(l.upper())}"
        buttons.append(
            f'<button type="button" class="lang-btn" data-lang="{_e(l)}">{label} ({len(items)})</button>'
        )
        sections.append(
            f'<section class="lang-section" data-lang="{_e(l)}">'
            f'<h2 class="lang-title">{label}</h2>'
            + ("".join(items) if items else '<div class="empty">Замечаний к исправлению нет.</div>')
            + "</section>"
        )
    body = (
        "<h1>Замечания по переводу — все языки</h1>"
        f'<div class="muted">{_e(filename)}</div>'
        '<div class="lang-bar"><button type="button" class="lang-btn active" data-lang="all">Все</button>'
        + "".join(buttons)
        + '<button type="button" id="copy-lang-link" class="copy-link">🔗 Скопировать ссылку на этот язык</button></div>'
        f'<p class="muted">{_intro(total, pending_total)}</p>'
        + _SAVE_LOGIN_HTML
        + "".join(sections)
    )
    return _page("Замечания — все языки", body, _SCRIPT + _LANG_SWITCH_SCRIPT, nonce)


_LANG_SWITCH_SCRIPT = """
(function () {
  var btns = document.querySelectorAll(".lang-btn");
  if (!btns.length) return;
  function show(lang, push) {
    var known = false;
    btns.forEach(function (b) { if (b.getAttribute("data-lang") === lang) known = true; });
    if (!known) lang = "all";
    btns.forEach(function (b) { b.classList.toggle("active", b.getAttribute("data-lang") === lang); });
    document.querySelectorAll(".lang-section").forEach(function (s) {
      s.hidden = lang !== "all" && s.getAttribute("data-lang") !== lang;
    });
    var copy = document.getElementById("copy-lang-link");
    if (copy) copy.textContent = lang === "all" ? "🔗 Скопировать ссылку на все языки" : "🔗 Скопировать ссылку на этот язык";
    if (push) {
      var url = location.pathname + location.search + (lang === "all" ? "" : "#" + encodeURIComponent(lang));
      history.replaceState(null, "", url);
    }
  }
  btns.forEach(function (b) {
    b.addEventListener("click", function () { show(b.getAttribute("data-lang"), true); window.scrollTo(0, 0); });
  });
  var copy = document.getElementById("copy-lang-link");
  if (copy) copy.addEventListener("click", function () {
    var done = function () { var t = copy.textContent; copy.textContent = "✓ Ссылка скопирована"; setTimeout(function () { copy.textContent = t; }, 1500); };
    try { navigator.clipboard.writeText(location.href).then(done, done); } catch (e) { done(); }
  });
  window.addEventListener("hashchange", function () { show(decodeURIComponent(location.hash.slice(1)) || "all", false); });
  show(decodeURIComponent(location.hash.slice(1)) || "all", false);
})();
"""
