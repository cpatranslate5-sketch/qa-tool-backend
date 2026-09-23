"""Quick local smoke test for the shared-folders / admin model and the
check types (register is now a plain report, not a document-gated
pass/fail — see app.claude_client's register_value machinery). Uses
a throwaway SQLite DB (no DATABASE_URL set) and no AI key, so only
rule-based findings are expected, not AI ones."""
import asyncio
import io
import os
import sys

os.environ["DATABASE_URL"] = ""

sys.path.insert(0, os.path.dirname(__file__))

import app.database as dbmod
dbmod.engine = dbmod.create_engine("sqlite:///./qa_tool_smoketest.db", connect_args={"check_same_thread": False})
dbmod.SessionLocal = dbmod.sessionmaker(autocommit=False, autoflush=False, bind=dbmod.engine)
if os.path.exists("qa_tool_smoketest.db"):
    os.remove("qa_tool_smoketest.db")
dbmod.init_db()

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def check(label, resp, expect=200):
    status = "OK" if resp.status_code == expect else "FAIL"
    print(f"[{status}] {label} -> {resp.status_code}")
    if status == "FAIL":
        print("   body:", resp.text[:500])
    return resp

# --- folder creation: first ever becomes admin ---
r = check("create folder Александр (first -> admin)", client.post("/managers", json={"name": "Александр", "code": "1234"}))
admin_id = r.json()["id"]
assert r.json()["is_admin"] is True

r = check("create folder Мария (second -> not admin)", client.post("/managers", json={"name": "Мария", "code": "5678"}))
regular_id = r.json()["id"]
assert r.json()["is_admin"] is False

check("duplicate folder name", client.post("/managers", json={"name": "Александр", "code": "0000"}), expect=409)

# --- listing folders (public, no code) — admin folder always first
# (point 2 of Александр's folder-access spec) ---
r = check("list managers", client.get("/managers"))
assert len(r.json()) == 2
assert r.json()[0]["is_admin"] is True and r.json()[0]["name"] == "Александр"

# --- unlock flow ---
check("unlock wrong code", client.post(f"/managers/{regular_id}/unlock", json={"code": "wrong"}), expect=401)
check("unlock correct code", client.post(f"/managers/{regular_id}/unlock", json={"code": "5678"}))

# --- admin bypass: whoever already has admin access on this device can
# open any other folder without typing that folder's own password ---
check("admin-enter refuses a non-admin claimed id", client.post(
    f"/managers/{regular_id}/admin-enter", json={"admin_manager_id": regular_id}
), expect=403)
r = check("real admin opens Мария's folder with no password", client.post(
    f"/managers/{regular_id}/admin-enter", json={"admin_manager_id": admin_id}
))
assert r.json()["id"] == regular_id and r.json()["name"] == "Мария"

# --- password change: available to every folder, not just admin ---
check("wrong current password rejected", client.post(
    f"/managers/{regular_id}/change-password", json={"current_code": "nope", "new_code": "9999"}
), expect=401)
check("password change succeeds", client.post(
    f"/managers/{regular_id}/change-password", json={"current_code": "5678", "new_code": "9999"}
))
check("old password no longer works", client.post(f"/managers/{regular_id}/unlock", json={"code": "5678"}), expect=401)
check("new password works", client.post(f"/managers/{regular_id}/unlock", json={"code": "9999"}))

# --- non-admin blocked from structural changes ---
check("non-admin create project blocked", client.post("/projects", json={"name": "Pragmatic Play Promo", "manager_id": regular_id}), expect=403)

# --- admin creates project ---
r = check("admin create project", client.post("/projects", json={"name": "Pragmatic Play Promo", "manager_id": admin_id}))
project_id = r.json()["id"]
assert r.json()["created_by_name"] == "Александр"

check("duplicate project name", client.post("/projects", json={"name": "Pragmatic Play Promo", "manager_id": admin_id}), expect=409)

# --- no more language folders: everything runs directly against the project ---
import openpyxl

# --- register no longer needs any document at all (removed 2026-09-16 —
# see models.Project's docstring): it used to 400 here with no Тон
# обращения doc uploaded, now it just runs and reports the register it
# finds, like any other check ---
check("register check runs fine with no document at all (nothing to gate it any more)", client.post("/check", json={
    "source": "Play now.",
    "translation": "Играйте сейчас.",
    "checks": ["register"],
    "project_id": project_id,
    "source_lang": "en",
    "target_lang": "ru",
    "manager_name": "Мария",
    "manager_id": regular_id,
}))

# --- known languages (the checkbox catalog) starts empty and is built up
# manually, one explicit add at a time ---
r = check("known languages starts empty", client.get(f"/projects/{project_id}/known-languages"))
assert r.json()["languages"] == [], r.json()

# --- admin builds the catalog explicitly, one language at a time ---
check("non-admin can't add a catalog language", client.post(
    f"/projects/{project_id}/languages", json={"manager_id": regular_id, "lang_code": "ru"}
), expect=403)
for code in ["ru", "es-mx", "kz", "en"]:
    check(f"admin adds '{code}' to the catalog", client.post(
        f"/projects/{project_id}/languages", json={"manager_id": admin_id, "lang_code": code}
    ))
# re-adding an already-present language is a harmless no-op, not an error
check("re-adding an existing catalog language is a no-op", client.post(
    f"/projects/{project_id}/languages", json={"manager_id": admin_id, "lang_code": "ru"}
))
r = check("known languages now reflects the manually-built catalog", client.get(f"/projects/{project_id}/known-languages"))
assert set(r.json()["languages"]) == {"ru", "es-mx", "kz", "en"}, r.json()

# --- admin can drop a single straggler language from the catalog without
# touching the rest — for exactly the situation this feature was built
# for: a project created via "copy from an existing project" (tested
# further below) inherits that other project's whole catalog, including
# a language nobody meant for THIS project ---
check("non-admin can't delete a catalog language", client.delete(
    f"/projects/{project_id}/languages/kz", params={"manager_id": regular_id}
), expect=403)
check("deleting a language not in the catalog 404s", client.delete(
    f"/projects/{project_id}/languages/zz", params={"manager_id": admin_id}
), expect=404)
# uppercase on the way in, to prove the match is case-insensitive (the
# frontend always displays codes upper-cased) even though it's stored
# lower-cased
r = check("admin deletes the stray 'kz' catalog language", client.delete(
    f"/projects/{project_id}/languages/KZ", params={"manager_id": admin_id}
))
assert set(r.json()["languages"]) == {"ru", "es-mx", "en"}, r.json()
r = check("known languages no longer include the deleted one", client.get(f"/projects/{project_id}/known-languages"))
assert set(r.json()["languages"]) == {"ru", "es-mx", "en"}, r.json()

# --- regression: exactly what Александр hit live — he added a BARE
# language ("es") to the catalog alongside an already-present
# region-qualified variant of the same base ("es-mx") and it vanished
# from known-languages entirely, looking like the add did nothing.
# Root cause: _catalog_languages ran the stored list through
# merge_lang_codes, a helper meant for collapsing ambiguous guesses
# gathered automatically from a document (where a bare "ko" and a
# region-qualified "ko-KR" really are the same physical column) — wrong
# for the catalog, which is a manager's own deliberate, one-at-a-time
# list where a bare "es" alongside "es-ar"/"es-mx" is a genuinely
# separate, intentional third entry. Fixed: the catalog now shows
# exactly what was added, nothing collapsed away. ---
check("admin adds bare 'es' to the catalog alongside the existing 'es-mx'", client.post(
    f"/projects/{project_id}/languages", json={"manager_id": admin_id, "lang_code": "es"}
))
r = check("bare 'es' is NOT silently merged away just because 'es-mx' also exists", client.get(f"/projects/{project_id}/known-languages"))
assert set(r.json()["languages"]) == {"ru", "es-mx", "en", "es"}, r.json()
check("clean up: remove 'es' from the catalog again", client.delete(
    f"/projects/{project_id}/languages/es", params={"manager_id": admin_id}
))
r = check("catalog back to its prior state after cleanup", client.get(f"/projects/{project_id}/known-languages"))
assert set(r.json()["languages"]) == {"ru", "es-mx", "en"}, r.json()
print("[OK] the catalog shows exactly what was manually added or removed, never auto-merged away")

# --- GLOBAL language-alias dictionary (Александр's own idea): unlike the
# per-project catalog above, this is shared across the whole platform and
# deliberately open to every folder, not just the admin one — a wrong or
# redundant alias is low-stakes and self-correcting (everyone sees who
# added what), unlike the catalog which decides what actually gets
# checked/billed ---
r = check("language-aliases starts empty", client.get("/language-aliases"))
assert r.json()["aliases"] == [], r.json()
check("a non-admin folder CAN add an alias (deliberately not admin-gated)", client.post(
    "/language-aliases", json={"manager_id": regular_id, "alias": "GEO", "canonical_code": "ka"}
))
r = check("the new alias shows up, stored lower-cased, with who added it", client.get("/language-aliases"))
aliases = r.json()["aliases"]
assert len(aliases) == 1, aliases
assert aliases[0]["alias"] == "geo", aliases
assert aliases[0]["canonical_code"] == "ka", aliases
assert aliases[0]["added_by_name"] == "Мария", aliases  # regular_id's folder name
geo_alias_id = aliases[0]["id"]
check("adding the same alias again is refused, not silently overwritten", client.post(
    "/language-aliases", json={"manager_id": admin_id, "alias": "geo", "canonical_code": "ru"}
), expect=409)
check("a non-admin folder CAN delete an alias too (same open-access rule)", client.delete(
    f"/language-aliases/{geo_alias_id}", params={"manager_id": regular_id}
))
r = check("deleted alias no longer listed", client.get("/language-aliases"))
assert r.json()["aliases"] == [], r.json()
check("deleting an alias that's already gone 404s", client.delete(
    f"/language-aliases/{geo_alias_id}", params={"manager_id": regular_id}
), expect=404)

# --- the actual point of the feature: a taught alias rescues a column that
# would otherwise land in unrecognized_columns (a raw label with a space
# in it, which no amount of regex tuning could ever guess at on its own) ---
check("teach the platform 'PORTUGUESE BRAZIL' means Brazilian Portuguese", client.post(
    "/language-aliases", json={"manager_id": admin_id, "alias": "Portuguese Brazil", "canonical_code": "pt-br"}
))
alias_wb = openpyxl.Workbook()
alias_ws = alias_wb.active
alias_ws.append(["Context", "en", "ru", "Portuguese Brazil"])
alias_ws.append(["Greeting", "Hello", "Привет", "Olá"])
alias_buf = io.BytesIO()
alias_wb.save(alias_buf)
alias_buf.seek(0)
r = check("a taught alias with a space in it is now recognized as a language, not dropped as unrecognized", client.post(
    f"/projects/{project_id}/multi-check/detect-languages",
    files={"file": ("alias_test.xlsx", alias_buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
))
assert "Portuguese Brazil" not in r.json()["unrecognized_columns"], r.json()
assert "pt-br" in r.json()["unknown_languages"] or "pt-br" in r.json()["languages"], r.json()

# Regression: a taught alias must win even when its raw text happens to
# collide with an unrelated column-shape rule (a META_COL_NAMES entry like
# "статус", or the "label: number" limit-spec pattern) — parse_workbook used
# to check those shortcuts BEFORE consulting the alias map, so a taught
# alias whose text matched one would be silently dropped, never even
# reaching unrecognized_columns for the manager to notice. Now checked first.
check("teach the platform 'статус' (normally a meta/status column) means Georgian", client.post(
    "/language-aliases", json={"manager_id": admin_id, "alias": "статус", "canonical_code": "ka"}
))
meta_alias_wb = openpyxl.Workbook()
meta_alias_ws = meta_alias_wb.active
meta_alias_ws.append(["Context", "en", "статус"])
meta_alias_ws.append(["Greeting", "Hello", "გამარჯობა"])
meta_alias_buf = io.BytesIO()
meta_alias_wb.save(meta_alias_buf)
meta_alias_buf.seek(0)
r = check("a taught alias whose text collides with a meta-column name is still recognized as a language", client.post(
    f"/projects/{project_id}/multi-check/detect-languages",
    files={"file": ("meta_alias_test.xlsx", meta_alias_buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
))
assert "статус" not in r.json()["unrecognized_columns"], r.json()
assert "ka" in r.json()["unknown_languages"] or "ka" in r.json()["languages"], r.json()
# clean up so this alias doesn't affect any later test in this file
aliases_now = client.get("/language-aliases").json()["aliases"]
for a in aliases_now:
    check(f"clean up: remove '{a['alias']}' alias again", client.delete(
        f"/language-aliases/{a['id']}", params={"manager_id": admin_id}
    ))
print("[OK] global /language-aliases: open to every folder (add + delete), rejects a duplicate "
      "alias instead of silently overwriting what it used to mean, and a taught spelling rescues "
      "a column that regex rules alone could never recognize (even one containing a space)")

# --- BOTH folders see the same shared project (no manager scoping) ---
r = check("list projects (shared)", client.get("/projects"))
assert len(r.json()) == 1

# --- non-admin CAN run a single check across every remaining check type ---
r = check("non-admin single check (ru)", client.post("/check", json={
    "source": "The bonus is $50 and expires in 3 days.",
    "translation": "Бонус составляет $500 и истекает через 3 дня",
    "checks": ["numbers", "placeholders", "register", "typo", "untranslatable", "completeness", "punctuation"],
    "project_id": project_id,
    "source_lang": "en",
    "target_lang": "ru",
    "manager_name": "Мария",
    "manager_id": regular_id,
}))
findings = r.json()["findings"]
print("   findings:", findings)
# number mismatch ($50 vs $500) must be caught by the rule-based check
assert any(f["type"] == "numbers" for f in findings)
# source ends in "." and translation doesn't -> punctuation rule should fire
assert any(f["type"] == "punctuation" for f in findings), findings

# --- double space is caught regardless of source ---
r = check("punctuation: double space", client.post("/check", json={
    "source": "Hello world.",
    "translation": "Привет  мир.",
    "checks": ["punctuation"],
    "manager_name": "Мария",
}))
findings = r.json()["findings"]
assert any(f["type"] == "punctuation" for f in findings), findings

# --- extra_instructions field is accepted and doesn't break anything ---
check("check with extra_instructions", client.post("/check", json={
    "source": "Play Golden Spin now!",
    "translation": "Играйте в Golden Spin сейчас!",
    "checks": ["untranslatable"],
    "project_id": project_id,
    "source_lang": "en",
    "target_lang": "ru",
    "extra_instructions": "В этой задаче 'Golden Spin' нужно переводить как 'Голден Спин'.",
    "manager_name": "Мария",
    "manager_id": regular_id,
}))

# --- history shows who performed it, keyed by project + folder (each
# manager only sees their own runs — point 1 of Александр's spec) ---
r = check("history shows attribution", client.get(f"/projects/{project_id}/history", params={"manager_id": regular_id}))
history = r.json()
assert len(history) == 3  # register-no-doc + full-checks + extra_instructions check (double-space was standalone)
assert history[0]["performed_by_name"] == "Мария"
assert history[0]["source_lang"] == "en" and history[0]["target_lang"] == "ru"
print("   performed_by_name:", history[0]["performed_by_name"])

# --- history is scoped per folder, not shared across every folder that
# touches the project ---
check("admin runs a check on the same shared project", client.post("/check", json={
    "source": "Hello.",
    "translation": "Привет.",
    "checks": ["punctuation"],
    "project_id": project_id,
    "source_lang": "en",
    "target_lang": "ru",
    "manager_name": "Александр",
    "manager_id": admin_id,
}))
r = check("Мария's history unaffected by admin's check", client.get(f"/projects/{project_id}/history", params={"manager_id": regular_id}))
assert len(r.json()) == 3, r.json()
r = check("admin's own history shows only admin's check", client.get(f"/projects/{project_id}/history", params={"manager_id": admin_id}))
assert len(r.json()) == 1, r.json()
assert r.json()[0]["performed_by_name"] == "Александр"

# --- deleting a single (point) check from history ---
single_check_to_delete = history[0]["id"]
check("admin can't delete Мария's single check (not theirs)", client.delete(
    f"/projects/{project_id}/history/{single_check_to_delete}", params={"manager_id": admin_id}
), expect=404)
check("Мария can delete her own single check", client.delete(
    f"/projects/{project_id}/history/{single_check_to_delete}", params={"manager_id": regular_id}
))
r = check("deleted single check no longer in history", client.get(
    f"/projects/{project_id}/history", params={"manager_id": regular_id}
))
assert len(r.json()) == 2, r.json()
assert all(h["id"] != single_check_to_delete for h in r.json()), r.json()

# --- non-admin CAN run a multi-check upload ---
sample_path = "/root/.claude/uploads/aee9e6e5-e96f-5b4b-aa4e-8aad6284c8c9/147efc1b-Promo_Rules_Localization.xlsx"
with open(sample_path, "rb") as f:
    r = check("non-admin multi-check upload", client.post(
        f"/projects/{project_id}/multi-check",
        files={"file": ("Promo_Rules_Localization.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"source_lang": "", "manager_name": "Мария", "manager_id": regular_id, "extra_instructions": ""},
    ))
multi_data = r.json()
multi_check_id = multi_data["multi_check_id"]
print("   summary:", multi_data["summary"])
# This sample file is above BATCH_THRESHOLD_CHARS (many languages), so it
# would normally go through the Message Batches path — but with no API key
# configured there's nothing to submit, so it finalizes immediately with
# just the rule-based findings, same as the old fully-synchronous behavior.
assert multi_data["status"] == "completed", multi_data
# --- a completed check reports when it started/finished, so the report
# (and history list) can show how long it actually took ---
assert multi_data["created_at"], multi_data
assert multi_data["completed_at"], multi_data
print("[OK] completed multi-check response includes created_at/completed_at")

# --- extend the catalog to a more realistic size before exercising
# detect-languages against the real sample file (which spans ~30
# languages) — mirrors an admin gradually building out their real
# language list over time, on top of the small set used above to test
# plain add/remove ---
for code in ["ar", "kk", "pt-br"]:
    check(f"admin adds '{code}' to the catalog", client.post(
        f"/projects/{project_id}/languages", json={"manager_id": admin_id, "lang_code": code}
    ))

# --- detect-languages: reports which of the file's language-shaped
# columns match the project's OWN catalog (checkable) vs. which merely
# LOOK like a language code but aren't on the manager's list at all
# (unknown_languages) — the fix for Александр's concrete bug report: a
# column literally labelled "PR" (meant as an abbreviation for
# Portuguese, but not a real code for it) used to get silently treated
# as a real target language, with Peru's flag, purely because it parsed
# as language-shaped. Now nothing enters the checkbox list just because
# a file happens to contain it. ---
with open(sample_path, "rb") as f:
    r = check("detect-languages splits catalog-matched vs unknown languages", client.post(
        f"/projects/{project_id}/multi-check/detect-languages",
        files={"file": ("Promo_Rules_Localization.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    ))
detected = r.json()["languages"]
unknown_detected = r.json()["unknown_languages"]
# every catalog language actually present in the file is reported as a
# real, checkable target...
assert {"ru", "es-mx", "en", "ar", "kk", "pt-br"} <= set(detected), detected
# ...while a real language column the manager simply hasn't added to
# their catalog yet (Bengali) is reported separately, not silently mixed
# into the checkable list
assert "bn" in unknown_detected, unknown_detected
assert r.json()["unrecognized_columns"] == [], r.json()  # this sample file has none of those
print(f"   detected (catalog-matched) languages: {detected}")
print(f"   unknown (language-shaped but not on the catalog) languages: {unknown_detected}")

# --- the exact scenario Александр reported: a column literally labelled
# "PR" (a manager's mistaken abbreviation for Portuguese) must come back
# as unknown — never silently added as a real target language ---
mislabel_wb = openpyxl.Workbook()
mislabel_ws = mislabel_wb.active
mislabel_ws.append(["Context", "en", "ru", "PR"])
mislabel_ws.append(["Greeting", "Hello", "Привет", "Olá"])
mislabel_buf = io.BytesIO()
mislabel_wb.save(mislabel_buf)
mislabel_buf.seek(0)
r = check("a mislabeled column ('PR' for Portuguese) is reported as unknown, not added silently", client.post(
    f"/projects/{project_id}/multi-check/detect-languages",
    files={"file": ("mislabeled_pr.xlsx", mislabel_buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
))
assert "pr" not in r.json()["languages"], r.json()
assert "pr" in r.json()["unknown_languages"], r.json()
assert "en" in r.json()["languages"] and "ru" in r.json()["languages"], r.json()

# --- exactly the fix Александр asked for: once the manager explicitly
# adds the (genuinely new) language to the catalog, the SAME file is
# re-checked and that column now counts as a recognized target — never
# automatically, only after the explicit add ---
check("admin adds the genuinely-new 'pr' language to the catalog", client.post(
    f"/projects/{project_id}/languages", json={"manager_id": admin_id, "lang_code": "pr"}
))
mislabel_buf.seek(0)
r = check("after adding it to the catalog, the same column is now recognized", client.post(
    f"/projects/{project_id}/multi-check/detect-languages",
    files={"file": ("mislabeled_pr.xlsx", mislabel_buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
))
assert "pr" in r.json()["languages"], r.json()
assert "pr" not in r.json()["unknown_languages"], r.json()
# clean up so 'pr' doesn't leak into later known-languages assertions
check("clean up: remove 'pr' from the catalog again", client.delete(
    f"/projects/{project_id}/languages/pr", params={"manager_id": admin_id}
))

# --- the Portuguese default: a column literally labelled bare "PT" (no
# region at all) must resolve straight to "pt-br", matching the "pt-br"
# already on this project's catalog (added above) — not show up as
# "unknown", and not stay bare "pt" either ---
pt_wb = openpyxl.Workbook()
pt_ws = pt_wb.active
pt_ws.append(["Context", "en", "ru", "PT"])
pt_ws.append(["Greeting", "Hello", "Привет", "Olá"])
pt_buf = io.BytesIO()
pt_wb.save(pt_buf)
pt_buf.seek(0)
r = check("a bare 'PT' column resolves to Brazilian Portuguese ('pt-br'), matching the catalog", client.post(
    f"/projects/{project_id}/multi-check/detect-languages",
    files={"file": ("bare_pt.xlsx", pt_buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
))
assert "pt-br" in r.json()["languages"], r.json()
assert "pt" not in r.json()["languages"], r.json()
assert "pt" not in r.json()["unknown_languages"] and "pt-br" not in r.json()["unknown_languages"], r.json()

# --- a column that isn't recognized as a language must be reported back
# BEFORE the manager presses "start", not only inside a finished report —
# by which point an AI-backed check may already have run without ever
# covering a genuine language column that got missed. ---
unrec_wb = openpyxl.Workbook()
unrec_ws = unrec_wb.active
unrec_ws.append(["Context", "en", "ru", "Notes for reviewer"])
unrec_ws.append(["Greeting", "Hello", "Привет", "double-check tone"])
unrec_buf = io.BytesIO()
unrec_wb.save(unrec_buf)
unrec_buf.seek(0)
r = check("detect-languages also reports unrecognized columns up front", client.post(
    f"/projects/{project_id}/multi-check/detect-languages",
    files={"file": ("with_notes_column.xlsx", unrec_buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
))
assert "Notes for reviewer" in r.json()["unrecognized_columns"], r.json()
assert "en" in r.json()["languages"] and "ru" in r.json()["languages"], r.json()

# --- a language code assigned to 2+ columns must also be reported back
# BEFORE the manager presses "start" — the manager's own ask, verbatim:
# "если платформа видит, что источника 2 или более и сомневается какой
# правильный, пусть сообщит об этом" (2026-09-22, right after a duplicated
# "ru" header was found live on his real file). Catching it here, at
# detect-languages, means before any AI-backed check has been paid for —
# the same warning is ALSO shown after a check runs (see the
# _duplicate_language_warning coverage above), in case it's missed here. ---
dup_detect_wb = openpyxl.Workbook()
dup_detect_ws = dup_detect_wb.active
dup_detect_ws.append(["Context", "en", "ru", "de", "ru"])
dup_detect_ws.append(["Greeting", "Hello", "Привет", "Hallo", "Привет-2"])
dup_detect_buf = io.BytesIO()
dup_detect_wb.save(dup_detect_buf)
dup_detect_buf.seek(0)
r = check("detect-languages also reports a duplicated language column up front", client.post(
    f"/projects/{project_id}/multi-check/detect-languages",
    files={"file": ("dup_lang.xlsx", dup_detect_buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
))
assert "ru" in r.json()["duplicate_languages"], r.json()
assert "C" in r.json()["duplicate_languages"]["ru"][0] and "E" in r.json()["duplicate_languages"]["ru"][0], r.json()
assert r.json()["languages"].count("ru") == 1, "a duplicated language must still be listed only once among 'languages'"

# --- and the control: a file with no duplicated columns must report none. ---
unrec_buf.seek(0)
r = check("detect-languages reports no duplicates for an ordinary, clean file", client.post(
    f"/projects/{project_id}/multi-check/detect-languages",
    files={"file": ("with_notes_column.xlsx", unrec_buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
), expect=200)
assert r.json()["duplicate_languages"] == {}, r.json()

# --- Александр hit this live right after the above shipped: his real file
# spells some region-qualified languages as "ES MX"/"PT BR" — the display
# style ("ES (MX)") without the parentheses — which fell through to the
# generic "looks like prose" rejection (any header with a space in it) and
# was reported as an unrecognized column even though it very much is a
# language. Deliberately ALL CAPS only, so an ordinary two-word column like
# "Task name" (mixed case in every real file) still correctly stays
# unrecognized rather than being guessed as a language. ---
space_wb = openpyxl.Workbook()
space_ws = space_wb.active
space_ws.append(["Context", "en", "ES MX", "PT BR", "Task name"])
space_ws.append(["Greeting", "Hello", "Hola", "Ola", "internal note"])
space_buf = io.BytesIO()
space_wb.save(space_buf)
space_buf.seek(0)
r = check("detect-languages recognizes ALL-CAPS \"ES MX\"/\"PT BR\"-style headers as languages", client.post(
    f"/projects/{project_id}/multi-check/detect-languages",
    files={"file": ("space_style_codes.xlsx", space_buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
))
assert "es-mx" in r.json()["languages"] and "pt-br" in r.json()["languages"], r.json()
assert "Task name" in r.json()["unrecognized_columns"], r.json()
assert "ES MX" not in r.json()["unrecognized_columns"], r.json()

# --- Александр's redesign: instead of the manager reviewing an
# auto-generated "here's what we found" list (easy to miss an absence
# from), the manager states which languages they expect up front, and
# verify-languages is the explicit per-language yes/no this drives —
# found via the same safe bridging as everywhere else (a code spelled
# differently still counts), missing only when nothing safely matches. ---
with open(sample_path, "rb") as f:
    r = check("verify-languages reports found/not-found per requested language", client.post(
        f"/projects/{project_id}/multi-check/verify-languages",
        files={"file": ("Promo_Rules_Localization.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"codes": "ru, es-mx, nl"},  # nl (Dutch) genuinely isn't in this sample file
    ))
verify_results = {row["code"]: row["found"] for row in r.json()["results"]}
assert verify_results == {"ru": True, "es-mx": True, "nl": False}, verify_results
# and the space-style header fix above is itself confirmed found through
# this same endpoint, not just through detect-languages.
verify_space_buf = io.BytesIO()
space_wb.save(verify_space_buf)
verify_space_buf.seek(0)
r = check("verify-languages also recognizes the ALL-CAPS space-style headers", client.post(
    f"/projects/{project_id}/multi-check/verify-languages",
    files={"file": ("space_style_codes.xlsx", verify_space_buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    data={"codes": "es-mx, pt-br, de"},  # de genuinely isn't in this file
))
verify_results2 = {row["code"]: row["found"] for row in r.json()["results"]}
assert verify_results2 == {"es-mx": True, "pt-br": True, "de": False}, verify_results2
print("[OK] verify-languages: explicit per-language found/not-found confirmation, driving "
      "the new \"tick what you expect, confirm, get told exactly what's missing\" flow")

# --- missing_from_sheets: a multi-sheet upload where a language is
# present on one sheet but missing from another — Александр hit this
# concretely testing the source-language confirmation (2026-09-17): he
# renamed "RU" on only one of two sheets, and "found" correctly stayed
# True (ru genuinely is still in the file), but that alone hid that one
# whole sheet had silently lost RU coverage. missing_from_sheets exists so
# the frontend's SOURCE-language confirmation (which needs every sheet,
# not just "somewhere in the file") can catch exactly this, while the
# existing target-language confirmation keeps reading only `found` and is
# completely unaffected. ---
_two_sheet_wb = openpyxl.Workbook()
_ws1 = _two_sheet_wb.active
_ws1.title = "Posts"
_ws1.append(["Context", "ru", "en"])
_ws1.append(["greeting", "Привет", "Hi"])
_ws2 = _two_sheet_wb.create_sheet("Designers")
_ws2.append(["Context", "ru", "en", "fr"])
_ws2.append(["greeting", "Привет", "Hi", "Salut"])
_two_sheet_buf = io.BytesIO()
_two_sheet_wb.save(_two_sheet_buf)
_two_sheet_buf.seek(0)
r = check("verify-languages: missing_from_sheets names exactly the sheet(s) lacking a language, "
          "not just whether it's in the file ANYWHERE", client.post(
    f"/projects/{project_id}/multi-check/verify-languages",
    files={"file": ("two_sheets.xlsx", _two_sheet_buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    data={"codes": "ru, fr"},
))
_two_sheet_results = {row["code"]: row for row in r.json()["results"]}
# "ru" is on both sheets — found, and missing from none.
assert _two_sheet_results["ru"]["found"] is True, _two_sheet_results
assert _two_sheet_results["ru"]["missing_from_sheets"] == [], _two_sheet_results
# "fr" is only on "Designers" — still "found" overall (unchanged, looser
# behavior a target-language confirmation relies on), but explicitly
# named as missing from "Posts".
assert _two_sheet_results["fr"]["found"] is True, _two_sheet_results
assert _two_sheet_results["fr"]["missing_from_sheets"] == ["Posts"], _two_sheet_results
print("[OK] verify-languages: missing_from_sheets names exactly which sheet(s) a language is "
      "absent from, while `found` itself stays unchanged (present anywhere in the file is still "
      "enough) — the source-language confirmation on the frontend is what actually requires an "
      "empty missing_from_sheets list; the target-language one keeps ignoring this field entirely")

# --- target_langs filter: checking just 2 of the file's many languages
# should only touch those 2 in the summary ---
with open(sample_path, "rb") as f:
    r = check("multi-check with target_langs filter", client.post(
        f"/projects/{project_id}/multi-check",
        files={"file": ("Promo_Rules_Localization.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"source_lang": "", "manager_name": "Мария", "manager_id": regular_id, "extra_instructions": "", "target_langs": "ru,es-mx"},
    ))
filtered_data = r.json()
assert filtered_data["status"] == "completed", filtered_data
assert set(filtered_data["summary"]["languages_checked"]) == {"ru", "es-mx"}, filtered_data["summary"]
print("   filtered languages_checked:", filtered_data["summary"]["languages_checked"])

r = check("multi-check history shows attribution", client.get(
    f"/projects/{project_id}/multi-check", params={"manager_id": regular_id}
))
assert r.json()[0]["performed_by_name"] == "Мария"
assert r.json()[0]["status"] == "completed"

report_resp = check("multi-check report download", client.get(
    f"/projects/{project_id}/multi-check/{multi_check_id}/report.xlsx", params={"manager_id": regular_id}
))
# --- the report's first row should say how long the check took, now that
# Александр asked for the time spent to show up in the downloadable report
# too (not just the history list) ---
report_wb = openpyxl.load_workbook(io.BytesIO(report_resp.content))
report_ws = report_wb.active
report_first_cell = report_ws.cell(row=1, column=1).value
assert "заняла" in report_first_cell, report_first_cell
assert report_ws.cell(row=3, column=1).value == "Лист", "header row should follow the duration line + spacer"
print("[OK] downloaded report's first row states check duration:", report_first_cell)

# --- multi-check history/detail/report are also scoped per folder ---
r = check("admin's multi-check history is empty (Мария's uploads aren't his)", client.get(
    f"/projects/{project_id}/multi-check", params={"manager_id": admin_id}
))
assert r.json() == [], r.json()
check("admin can't fetch Мария's multi-check detail by id", client.get(
    f"/projects/{project_id}/multi-check/{multi_check_id}", params={"manager_id": admin_id}
), expect=404)
check("admin can't download Мария's multi-check report", client.get(
    f"/projects/{project_id}/multi-check/{multi_check_id}/report.xlsx", params={"manager_id": admin_id}
), expect=404)

# --- regression: exactly what Александр hit live. His catalog checkbox
# for Portuguese was ticked as a bare "pt" (added before Portuguese
# started defaulting to Brazilian), but the file's own "PT" column now
# normalizes to "pt-br" — a plain string-equality filter would silently
# drop it from the actual check even though detect-languages had already
# told him it would be checked. The real run must bridge this the same
# way detect-languages does, never drop a language silently. ---
pt_check_wb = openpyxl.Workbook()
pt_check_ws = pt_check_wb.active
pt_check_ws.append(["Context", "en", "PT"])
pt_check_ws.append(["Greeting", "Hello", "Olá"])
pt_check_buf = io.BytesIO()
pt_check_wb.save(pt_check_buf)
pt_check_buf.seek(0)
r = check(
    "multi-check with an older, coarser target_langs spelling ('pt') still checks the file's actual 'pt-br' column",
    client.post(
        f"/projects/{project_id}/multi-check",
        files={"file": ("bare_pt_check.xlsx", pt_check_buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"source_lang": "en", "manager_name": "Мария", "manager_id": regular_id, "extra_instructions": "", "target_langs": "pt"},
    ),
)
pt_check_data = r.json()
assert pt_check_data["status"] == "completed", pt_check_data
assert "pt-br" in pt_check_data["summary"]["languages_checked"], pt_check_data["summary"]
assert "pt-br" in pt_check_data["sheets"][0]["languages"], pt_check_data["sheets"][0]

# --- a "DO NOT TRANSLATE" cell means this row is deliberately left
# untranslated for this language on purpose — it must be skipped entirely
# (no finding at all), not flagged as a numbers/content mismatch, even
# though the literal text would otherwise clearly disagree with the
# source. Александр's files legitimately need some rows translated for one
# language but not another. ---
dnt_wb = openpyxl.Workbook()
dnt_ws = dnt_wb.active
dnt_ws.append(["EN", "RU"])
dnt_ws.append(["Price: 500 dollars.", "Цена: 400 долларов."])  # genuine mismatch — must still be caught
dnt_ws.append(["Price: 500 dollars.", "DO NOT TRANSLATE"])
dnt_ws.append(["Price: 500 dollars.", " [do NOT Translate] "])  # brackets + mixed case variant
dnt_buf = io.BytesIO()
dnt_wb.save(dnt_buf)
dnt_buf.seek(0)
r = check("multi-check with DO NOT TRANSLATE cells", client.post(
    f"/projects/{project_id}/multi-check",
    files={"file": ("dnt.xlsx", dnt_buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    data={"source_lang": "en", "manager_name": "Мария", "manager_id": regular_id, "extra_instructions": "", "checks": "numbers"},
))
dnt_data = r.json()
assert dnt_data["status"] == "completed", dnt_data
dnt_ru_rows = dnt_data["sheets"][0]["languages"]["ru"]
assert len(dnt_ru_rows) == 1, dnt_ru_rows  # only the genuine mismatch row, both DNT rows skipped entirely
assert dnt_ru_rows[0]["translation"] == "Цена: 400 долларов.", dnt_ru_rows
assert any(f["type"] == "numbers" for f in dnt_ru_rows[0]["findings"]), dnt_ru_rows
print("   DO NOT TRANSLATE rows correctly skipped, genuine mismatch still caught:", dnt_ru_rows)

# --- Александр asked "стоимость 0$ с найденными проблемами — это
# нормально?" — yes, when only the free algorithmic checks are selected
# (no AI call ever happens, so nothing to bill), and checks_run in the
# response is exactly what lets the UI show which criteria actually ran
# instead of leaving him guessing. Covers both at once: run with only the
# free "punctuation" check (which is one of the CHECK_OPTIONS-labeled keys,
# unlike the plain "numbers"/"max_length" the frontend silently folds in),
# confirm cost stays 0 and checks_run reflects exactly that selection. ---
only_algo_wb = openpyxl.Workbook()
only_algo_ws = only_algo_wb.active
only_algo_ws.append(["EN", "RU"])
only_algo_ws.append(["Hello world.", "Привет мир"])  # source ends with "." but translation has no terminal punctuation at all -> reliably flagged
only_algo_buf = io.BytesIO()
only_algo_wb.save(only_algo_buf)
only_algo_buf.seek(0)
r = check("multi-check with only a free algorithmic criterion selected", client.post(
    f"/projects/{project_id}/multi-check",
    files={"file": ("only-algo.xlsx", only_algo_buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    data={"source_lang": "en", "manager_name": "Мария", "manager_id": regular_id, "extra_instructions": "", "checks": "punctuation"},
))
only_algo_data = r.json()
assert only_algo_data["status"] == "completed", only_algo_data
assert only_algo_data["cost_usd"] == 0, only_algo_data  # no AI check type was even selected
assert only_algo_data["checks_run"] == ["punctuation"], only_algo_data
print("[OK] $0 cost with real findings is expected when only free/algorithmic criteria are selected; checks_run confirms which criteria ran")

# --- the downloadable report should let Александр filter by language using
# Excel's own column-filter control ("фильтр по языкам") ---
only_algo_report = check("report for the only-algo check downloads fine", client.get(
    f"/projects/{project_id}/multi-check/{only_algo_data['multi_check_id']}/report.xlsx", params={"manager_id": regular_id}
))
only_algo_wb_read = openpyxl.load_workbook(io.BytesIO(only_algo_report.content))
only_algo_ws_read = only_algo_wb_read.active
# The filter range must start exactly ON the header row ("Лист", "Строка в
# файле", ...), not one row early on the blank spacer above it — an
# earlier version of this got that boundary wrong and silently excluded
# the header (and included the empty spacer instead) from the filterable
# range.
header_row_idx = next(
    r for r in range(1, only_algo_ws_read.max_row + 1)
    if only_algo_ws_read.cell(row=r, column=1).value == "Лист"
)
assert only_algo_ws_read.auto_filter.ref == f"A{header_row_idx}:I{only_algo_ws_read.max_row}", (
    only_algo_ws_read.auto_filter.ref, header_row_idx, only_algo_ws_read.max_row
)
print("[OK] downloaded report's Excel column filter starts exactly on the header row:", only_algo_ws_read.auto_filter.ref)

# --- deleting a multi-check report/upload from history ---
check("Мария can delete her own multi-check", client.delete(
    f"/projects/{project_id}/multi-check/{multi_check_id}", params={"manager_id": regular_id}
))
r = check("deleted multi-check no longer in history", client.get(
    f"/projects/{project_id}/multi-check", params={"manager_id": regular_id}
))
assert all(h["id"] != multi_check_id for h in r.json()), r.json()
check("deleted multi-check detail is gone", client.get(
    f"/projects/{project_id}/multi-check/{multi_check_id}", params={"manager_id": regular_id}
), expect=404)
check("admin can't delete Мария's multi-check (not theirs)", client.delete(
    f"/projects/{project_id}/multi-check/{dnt_data['multi_check_id']}", params={"manager_id": admin_id}
), expect=404)

# --- large multi-check actually goes through the Message Batches path when
# a batch can be submitted — simulate that here (no real Anthropic key in
# this sandbox) by faking the three network calls, to prove the
# submit -> poll -> merge-AI-findings-into-the-rule-based-skeleton pipeline
# actually produces a correct, complete result. ---
import app.excel_multi as excel_multi_mod

# custom_id is deliberately opaque (see build_batch_plan — it's built from
# the sheet/position index only, never from the language code itself, so a
# weird character in a file's own language column can never produce an
# invalid custom_id and get the whole batch rejected by Anthropic). This
# fake captures whatever custom_ids were actually submitted rather than
# hardcoding the old "s{sheet}-{lang}" shape, so the test doesn't silently
# stop verifying anything if that internal scheme ever changes again.
_submitted_custom_ids: list[str] = []


async def _fake_create_message_batch(requests):
    assert requests, "expected at least one per-language batch request to be built"
    _submitted_custom_ids.clear()
    _submitted_custom_ids.extend(r["custom_id"] for r in requests)
    return "msgbatch_test123"


_batch_status_call_count = {"n": 0}


async def _fake_get_batch_status(batch_id):
    assert batch_id == "msgbatch_test123"
    _batch_status_call_count["n"] += 1
    if _batch_status_call_count["n"] == 1:
        # First check (triggered by the history list's own opportunistic
        # poll, below) — still running, so it stays "processing" with real
        # counts instead of finalizing right there.
        return {"processing_status": "in_progress", "request_counts": {
            "processing": 5, "succeeded": 3, "errored": 0, "canceled": 0, "expired": 0,
        }}
    return {"processing_status": "ended", "results_url": "fake://results"}


async def _fake_get_batch_results(results_url):
    assert results_url == "fake://results"
    # Same fake AI finding for every custom_id that was actually submitted —
    # shape matches the real get_batch_results: {custom_id: {"text": ...,
    # "usage": ..., "stop_reason": ..., "result_type": ...}}. Every
    # language's rows are identical in this sample file's languages, so
    # this reliably shows up under "ru" (and every other language) without
    # the test needing to know its exact custom_id.
    assert _submitted_custom_ids, "expected _fake_create_message_batch to have run first"
    return {
        cid: {
            "text": '[{"row": 1, "type": "typo", "severity": "medium", "message": "тестовая ИИ-находка"}]',
            "usage": {"input_tokens": 1000, "output_tokens": 200},
            "stop_reason": "end_turn",
            "result_type": "succeeded",
        }
        for cid in _submitted_custom_ids
    }


excel_multi_mod.create_message_batch = _fake_create_message_batch
excel_multi_mod.get_batch_status = _fake_get_batch_status
excel_multi_mod.get_batch_results = _fake_get_batch_results

with open(sample_path, "rb") as f:
    r = check("large multi-check submits as a batch", client.post(
        f"/projects/{project_id}/multi-check",
        files={"file": ("Promo_Rules_Localization.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"source_lang": "", "manager_name": "Мария", "manager_id": regular_id, "extra_instructions": ""},
    ))
batch_multi_data = r.json()
assert batch_multi_data["status"] == "processing", batch_multi_data
batch_multi_check_id = batch_multi_data["multi_check_id"]
# Submission response already knows the total request count for free (no
# Anthropic call needed to say "0 of N so far").
assert batch_multi_data["progress"]["done"] == 0, batch_multi_data
assert batch_multi_data["progress"]["total"] > 0, batch_multi_data
# The UI falls back to showing elapsed waiting time whenever Anthropic's own
# counts haven't moved yet (Александр found the percentage looked frozen at
# 0% for a long stretch) — needs a real timestamp to compute that from.
assert batch_multi_data["created_at"], batch_multi_data

r = check("history shows the batch entry as processing", client.get(
    f"/projects/{project_id}/multi-check", params={"manager_id": regular_id}
))
hist_entry = r.json()[0]
assert hist_entry["status"] == "processing", hist_entry
# The history list opportunistically polls Anthropic itself (one call per
# still-processing upload) so a manager sees real progress — "готово X из
# Y" — without having to open that specific check first.
assert hist_entry["progress"] == {"done": 3, "total": 8}, hist_entry

check("report download blocked while processing", client.get(
    f"/projects/{project_id}/multi-check/{batch_multi_check_id}/report.xlsx", params={"manager_id": regular_id}
), expect=409)

# First poll: our fake get_batch_status already says "ended", so this same
# call both notices completion and merges the results in.
r = check("polling finalizes the batch", client.get(
    f"/projects/{project_id}/multi-check/{batch_multi_check_id}", params={"manager_id": regular_id}
))
finalized = r.json()
assert finalized["status"] == "completed", finalized
ru_findings = finalized["sheets"][0]["languages"].get("ru", [])
assert any(
    any(f["type"] == "typo" and "тестовая ИИ-находка" in f["message"] for f in row["findings"])
    for row in ru_findings
), ru_findings
print("   ru findings after batch merge:", ru_findings)
# The batch's real Anthropic usage (faked above) must translate into a
# non-zero cost, computed with the batch discount, and persisted on the
# record (not just present in the one-off response).
assert finalized["cost_usd"] > 0, finalized
# A batch-processed check finalizes asynchronously (unlike the synchronous
# path checked earlier), so completed_at has to be set separately, right
# here at finalization time — confirm that actually happened, not just for
# the synchronous path.
assert finalized["created_at"], finalized
assert finalized["completed_at"], finalized
print("[OK] finalized batch check also gets created_at/completed_at (not just the synchronous path)")
r2 = check("multi-check detail re-fetch still shows the persisted cost", client.get(
    f"/projects/{project_id}/multi-check/{batch_multi_check_id}", params={"manager_id": regular_id}
))
assert r2.json()["cost_usd"] == finalized["cost_usd"], r2.json()
print(f"   batch cost_usd: {finalized['cost_usd']}")

r = check("history now shows completed", client.get(
    f"/projects/{project_id}/multi-check", params={"manager_id": regular_id}
))
assert r.json()[0]["status"] == "completed"
assert r.json()[0]["completed_at"], r.json()[0]

check("report download works once completed", client.get(
    f"/projects/{project_id}/multi-check/{batch_multi_check_id}/report.xlsx", params={"manager_id": regular_id}
))

# --- while a batch is still "in_progress" (not yet "ended"), the detail
# endpoint surfaces Anthropic's own request_counts as {"done", "total"}
# instead of finalizing — this is what lets the UI show a real progress
# readout ("2 of 5 done") rather than a guessed time estimate, which
# Anthropic's API doesn't provide at all. ---
_progress_poll_count = {"n": 0}


async def _fake_get_batch_status_progress(batch_id):
    _progress_poll_count["n"] += 1
    if _progress_poll_count["n"] == 1:
        # still running: one request finished, none failed yet
        return {"processing_status": "in_progress", "request_counts": {
            "processing": 0, "succeeded": 1, "errored": 0, "canceled": 0, "expired": 0,
        }}
    return {"processing_status": "ended", "results_url": "fake://results"}


excel_multi_mod.get_batch_status = _fake_get_batch_status_progress

with open(sample_path, "rb") as f:
    r = check("second large multi-check submits as a batch (for progress test)", client.post(
        f"/projects/{project_id}/multi-check",
        files={"file": ("Promo_Rules_Localization.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"source_lang": "", "manager_name": "Мария", "manager_id": regular_id, "extra_instructions": ""},
    ))
progress_multi_data = r.json()
assert progress_multi_data["status"] == "processing", progress_multi_data
progress_multi_check_id = progress_multi_data["multi_check_id"]

r = check("polling while still in-progress reports live counts instead of finalizing", client.get(
    f"/projects/{project_id}/multi-check/{progress_multi_check_id}", params={"manager_id": regular_id}
))
mid_poll = r.json()
assert mid_poll["status"] == "processing", mid_poll
assert mid_poll["progress"] == {"done": 1, "total": 1}, mid_poll
assert mid_poll["created_at"], mid_poll

r = check("next poll finalizes once Anthropic marks the batch ended", client.get(
    f"/projects/{project_id}/multi-check/{progress_multi_check_id}", params={"manager_id": regular_id}
))
assert r.json()["status"] == "completed", r.json()

excel_multi_mod.get_batch_status = _fake_get_batch_status

# --- cancelling a still-processing upload (Александр asked whether a check
# can be stopped mid-way — e.g. it's taking longer than expected and he'd
# rather re-upload with "Срочно", or he simply changed his mind). Deleting a
# still-processing entry now doubles as "cancel": Anthropic is told to stop
# working on it (so it isn't billed for whatever hadn't started yet) before
# our own record is dropped. ---
_cancel_calls_seen = []


async def _fake_cancel_message_batch(batch_id):
    _cancel_calls_seen.append(batch_id)
    return {"processing_status": "canceling"}


excel_multi_mod.cancel_message_batch = _fake_cancel_message_batch

with open(sample_path, "rb") as f:
    r = check("third large multi-check submits as a batch (for cancel test)", client.post(
        f"/projects/{project_id}/multi-check",
        files={"file": ("Promo_Rules_Localization.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"source_lang": "", "manager_name": "Мария", "manager_id": regular_id, "extra_instructions": ""},
    ))
cancel_multi_data = r.json()
assert cancel_multi_data["status"] == "processing", cancel_multi_data
cancel_multi_check_id = cancel_multi_data["multi_check_id"]

check("cancelling (deleting) a still-processing upload tells Anthropic to stop", client.delete(
    f"/projects/{project_id}/multi-check/{cancel_multi_check_id}", params={"manager_id": regular_id}
))
assert _cancel_calls_seen == ["msgbatch_test123"], _cancel_calls_seen
r = check("cancelled upload no longer in history", client.get(
    f"/projects/{project_id}/multi-check", params={"manager_id": regular_id}
))
assert all(h["id"] != cancel_multi_check_id for h in r.json()), r.json()

# --- rough ETA for a still-processing batch job, learned from how long
# past jobs of a similar size actually took (Александр asked for some kind
# of estimate instead of only elapsed time). A real test run finishes in
# milliseconds, far too fast to naturally produce any meaningful "minutes
# elapsed" — so two fake historical completed batch jobs are inserted
# directly via the DB with known size and duration, and the estimate for a
# new job is checked against the rate they imply. ---
from app.main import _estimate_batch_minutes
from app import models
import datetime as _dt

_hist_db = dbmod.SessionLocal()
try:
    _now_utc = _dt.datetime.now(_dt.timezone.utc)
    _hist_db.add(models.MultiCheck(
        project_id=project_id, filename="hist1.xlsx", source_lang="en",
        status="completed", batch_id="msgbatch_hist1", manager_id=regular_id,
        batch_volume_chars=10_000,
        created_at=_now_utc - _dt.timedelta(minutes=10), completed_at=_now_utc,
    ))
    _hist_db.add(models.MultiCheck(
        project_id=project_id, filename="hist2.xlsx", source_lang="en",
        status="completed", batch_id="msgbatch_hist2", manager_id=regular_id,
        batch_volume_chars=20_000,
        created_at=_now_utc - _dt.timedelta(minutes=20), completed_at=_now_utc,
    ))
    _hist_db.commit()
    # Pooled rate: (10,000 + 20,000) chars over (10 + 20) minutes = 1,000
    # chars/minute — NOT the average of the two jobs' own ratios (which
    # would also happen to be 1,000/min here; the point of pooling is that a
    # small, fast outlier can't dominate the average the way it would if
    # every job's ratio counted equally regardless of size).
    assert _estimate_batch_minutes(_hist_db, 5_000) == 5, _estimate_batch_minutes(_hist_db, 5_000)
    assert _estimate_batch_minutes(_hist_db, 0) is None
finally:
    _hist_db.close()
print("[OK] _estimate_batch_minutes: pools characters/minutes across recent finished batch jobs into "
      "one rate to estimate a new job's duration, rather than averaging each job's own ratio")

with open(sample_path, "rb") as f:
    r = check("multi-check submission now includes a rough ETA once there's history to learn from", client.post(
        f"/projects/{project_id}/multi-check",
        files={"file": ("Promo_Rules_Localization.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"source_lang": "", "manager_name": "Мария", "manager_id": regular_id, "extra_instructions": ""},
    ))
eta_multi_data = r.json()
assert eta_multi_data["status"] == "processing", eta_multi_data
assert eta_multi_data["estimated_minutes"] is not None and eta_multi_data["estimated_minutes"] > 0, eta_multi_data
print("   estimated_minutes:", eta_multi_data["estimated_minutes"])
check("cancel that ETA-test upload so it doesn't linger in history for later tests", client.delete(
    f"/projects/{project_id}/multi-check/{eta_multi_data['multi_check_id']}", params={"manager_id": regular_id}
))

# --- "Срочно" (urgent) flag forces the live/synchronous path even for a
# file whose volume would otherwise route it to the batch queue. The batch
# network calls are still monkeypatched from above, but the urgent path
# must never call them — it should complete in the same request instead of
# coming back as "processing". ---
_batch_calls_seen = []
_original_fake_create_batch = excel_multi_mod.create_message_batch


async def _fake_create_message_batch_tracking(requests):
    _batch_calls_seen.append(requests)
    return await _original_fake_create_batch(requests)


excel_multi_mod.create_message_batch = _fake_create_message_batch_tracking

with open(sample_path, "rb") as f:
    r = check("urgent multi-check bypasses the batch queue", client.post(
        f"/projects/{project_id}/multi-check",
        files={"file": ("Promo_Rules_Localization.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={
            "source_lang": "",
            "manager_name": "Мария",
            "manager_id": regular_id,
            "extra_instructions": "",
            "urgent": "true",
        },
    ))
urgent_multi_data = r.json()
assert urgent_multi_data["status"] == "completed", urgent_multi_data
assert not _batch_calls_seen, "urgent=true must not go through the batch queue at all"
excel_multi_mod.create_message_batch = _original_fake_create_batch

# --- Александр hit a real production bug: a file with a language column
# header like "fr-CI" but typed with a Cyrillic «с» (U+0441) instead of the
# visually-identical Latin "c" — looks completely normal to a human, but
# Anthropic's Batches API requires custom_id to match ^[a-zA-Z0-9_-]{1,64}$,
# and the old code built custom_id straight from the language string
# ("s{sheet}-{lang}"), so this one bad column got the ENTIRE batch (every
# language in it) rejected by Anthropic with a 400 — which, because it was
# an unhandled exception, came back to the browser as a raw 500 with no
# CORS headers, which Chrome then reported as "blocked by CORS policy",
# completely hiding the real cause. Fixed by building custom_id from the
# sheet/position index only (see build_batch_plan) — never from the
# language string. This proves that holds for ANY weird character, not
# just this one. ---
import re as _re
from app.excel_multi import build_batch_plan as _build_batch_plan_direct
from app.excel_multi import parse_workbook as _parse_workbook_direct

_weird_lang_wb = openpyxl.Workbook()
_weird_lang_ws = _weird_lang_wb.active
_weird_lang_ws.append(["EN", "RU", "fr-сi", "Ünïçø∂€ 漢字"])  # 3rd header: Cyrillic с, not Latin c
_weird_lang_ws.append(["Hello.", "Привет.", "Bonjour.", "Bonjour."])
_weird_lang_buf = io.BytesIO()
_weird_lang_wb.save(_weird_lang_buf)
_weird_lang_buf.seek(0)
_weird_sheets = _parse_workbook_direct(_weird_lang_buf.read())
_weird_requests, _weird_skeleton = _build_batch_plan_direct(_weird_sheets, "en", ["typo"], "", None)
_custom_id_pattern = _re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_bad_ids = [r["custom_id"] for r in _weird_requests if not _custom_id_pattern.match(r["custom_id"])]
assert not _bad_ids, f"custom_id must always be Anthropic-safe, regardless of the file's own language codes: {_bad_ids}"
assert len(_weird_requests) >= 2, "expected a request for each non-source language, weird characters included"
print(f"[OK] build_batch_plan: custom_id stays ASCII-safe even for language codes with lookalike/unicode characters (e.g. Cyrillic «с» instead of Latin \"c\"): {[r['custom_id'] for r in _weird_requests]}")

# --- and the defensive side of the same fix: an app-wide handler for
# httpx.HTTPError (registered in main.py, not the bare Exception class —
# see its comment for why that distinction is what actually makes CORS
# headers survive) means ANY Anthropic/network failure, anywhere AI checks
# are called, surfaces as a clean, readable error — never a raw crash that
# strips CORS headers and shows up in the browser as a confusing "blocked
# by CORS policy" message with no indication anything is actually wrong
# server-side. Covers all three places that call out to Anthropic:
# batch submission (the exact path Александр's real report hit), the
# live/synchronous multi-check path, and the standalone single-check
# endpoint. ---
import httpx as _httpx_for_fault_injection


async def _fake_create_message_batch_failing(requests):
    raise _httpx_for_fault_injection.ConnectError("simulated Anthropic outage")


_previous_create_batch = excel_multi_mod.create_message_batch
excel_multi_mod.create_message_batch = _fake_create_message_batch_failing
with open(sample_path, "rb") as f:
    r = check("a failed batch submission surfaces as a clean error, not a raw crash", client.post(
        f"/projects/{project_id}/multi-check",
        files={"file": ("Promo_Rules_Localization.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"source_lang": "", "manager_name": "Мария", "manager_id": regular_id, "extra_instructions": "", "checks": "typo"},
    ), expect=502)
assert "Не удалось связаться" in r.json()["detail"], r.json()
excel_multi_mod.create_message_batch = _previous_create_batch

# same fault, but on the live/synchronous path (small file, stays under
# BATCH_THRESHOLD_CHARS) — a different code path (run_ai_checks_batch ->
# _call_claude) than the batch-submission one above, so it needs its own
# coverage to actually prove the app-wide handler, not just this one
# call site.
import app.claude_client as claude_client_mod

_previous_call_claude = claude_client_mod._call_claude


async def _fake_call_claude_failing(prompt, model=None):
    raise _httpx_for_fault_injection.ConnectError("simulated Anthropic outage")


claude_client_mod._call_claude = _fake_call_claude_failing
small_wb = openpyxl.Workbook()
small_ws = small_wb.active
small_ws.append(["EN", "RU"])
small_ws.append(["Hello.", "Привет."])
small_buf = io.BytesIO()
small_wb.save(small_buf)
small_buf.seek(0)
r = check("an Anthropic failure on the LIVE multi-check path also surfaces cleanly", client.post(
    f"/projects/{project_id}/multi-check",
    files={"file": ("small.xlsx", small_buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    data={"source_lang": "en", "manager_name": "Мария", "manager_id": regular_id, "extra_instructions": "", "checks": "typo"},
), expect=502)
assert "Не удалось связаться" in r.json()["detail"], r.json()

# and on the standalone single-check endpoint
r = check("an Anthropic failure on the standalone /check endpoint also surfaces cleanly", client.post(
    "/check", json={"source": "Hello.", "translation": "Привет.", "checks": ["typo"]},
), expect=502)
assert "Не удалось связаться" in r.json()["detail"], r.json()
claude_client_mod._call_claude = _previous_call_claude

# --- and the flip side, deliberately: a genuinely unrelated bug (not an
# Anthropic/network failure) must NOT be silently swallowed by the same
# handler — it should still surface as the framework's normal bare 500, so
# a real bug still gets noticed and fixed rather than hidden behind a
# friendly "temporary outage" message forever. Locks in that the handler
# above is registered for httpx.HTTPError specifically, not bare
# Exception. ---
async def _fake_call_claude_unrelated_bug(prompt, model=None):
    raise RuntimeError("some unrelated real bug, not an Anthropic/network failure")


claude_client_mod._call_claude = _fake_call_claude_unrelated_bug
# TestClient's default client re-raises unhandled server exceptions into the
# test process instead of returning them as a response (so a real crash is
# loud in normal testing) — exactly the opposite of what we want to check
# here, so this one call uses its own client with that behaviour turned off.
_no_raise_client = TestClient(app, raise_server_exceptions=False)
r_bug = check(
    "an unrelated bug (not httpx.HTTPError) is NOT masked as a friendly outage message",
    _no_raise_client.post("/check", json={"source": "Hello.", "translation": "Привет.", "checks": ["typo"]}),
    expect=500,
)
assert "Не удалось связаться" not in r_bug.text, r_bug.text
claude_client_mod._call_claude = _previous_call_claude

# --- get_batch_results must not let one garbled .jsonl line (a cut-off
# download, a proxy hiccup) take down parsing of the rest of the batch's
# results — it should skip just that line and keep going. ---
class _FakeBatchResultsResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class _FakeBatchResultsClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        good_line = (
            '{"custom_id": "s0-t0", "result": {"type": "succeeded", '
            '"message": {"content": [{"type": "text", "text": "[]"}], '
            '"usage": {}, "stop_reason": "end_turn"}}}'
        )
        return _FakeBatchResultsResponse("\n".join([good_line, "{not valid json", good_line.replace("t0", "t1")]))


_previous_async_client = claude_client_mod.httpx.AsyncClient
claude_client_mod.httpx.AsyncClient = _FakeBatchResultsClient
_results = asyncio.get_event_loop().run_until_complete(claude_client_mod.get_batch_results("fake://results"))
claude_client_mod.httpx.AsyncClient = _previous_async_client
assert set(_results.keys()) == {"s0-t0", "s0-t1"}, _results
print("[OK] get_batch_results skips a malformed .jsonl line instead of crashing the whole batch")

# --- a bad/expired API key (401/403 from Anthropic) is a config problem on
# our side, not a transient outage — the handler should say so distinctly
# rather than telling the user to just try again in a minute. ---
class _FakeAuthErrorResponse:
    status_code = 401


async def _fake_call_claude_auth_error(prompt, model=None):
    raise _httpx_for_fault_injection.HTTPStatusError(
        "401 Unauthorized", request=None, response=_FakeAuthErrorResponse()
    )


claude_client_mod._call_claude = _fake_call_claude_auth_error
r_auth = check(
    "a 401 from Anthropic (bad/expired API key) gets a distinct 'not a transient outage' message",
    client.post("/check", json={"source": "Hello.", "translation": "Привет.", "checks": ["typo"]}),
    expect=502,
)
assert "ошибк" in r_auth.json()["detail"].lower() and "автор" in r_auth.json()["detail"].lower(), r_auth.json()
assert "временный сбой" not in r_auth.json()["detail"], r_auth.json()
claude_client_mod._call_claude = _previous_call_claude

# --- project template copy: new project starts with the same language
# catalog, fully independent afterward (editing one never touches the
# other) — used to also copy the tone-of-address doc, back when that
# existed (removed 2026-09-16, see models.Project's docstring) ---
r = check("create project copying requirements from the first", client.post(
    "/projects", json={"name": "LS Promo", "manager_id": admin_id, "copy_from_project_id": project_id}
))
copy_project_id = r.json()["id"]

# --- the copy must inherit the source project's language catalog —
# without this, a project created "from" a template would start with an
# empty, useless checkbox list ---
r = check("copied project's language catalog also matches source", client.get(f"/projects/{copy_project_id}/known-languages"))
assert set(r.json()["languages"]) == {"ru", "es-mx", "en", "ar", "kk", "pt-br"}, r.json()
# and it's a genuinely independent copy — removing a language from the
# COPY must not touch the original
check("removing a language from the copy", client.delete(
    f"/projects/{copy_project_id}/languages/kk", params={"manager_id": admin_id}
))
r = check("original project's catalog is untouched by the copy's edit", client.get(f"/projects/{project_id}/known-languages"))
assert "kk" in r.json()["languages"], r.json()

# --- project deletion requires the admin's password ---
check("delete project wrong password rejected", client.request(
    "DELETE", f"/projects/{copy_project_id}", json={"manager_id": admin_id, "code": "wrong"}
), expect=401)
check("delete project by non-admin rejected", client.request(
    "DELETE", f"/projects/{copy_project_id}", json={"manager_id": regular_id, "code": "9999"}
), expect=403)
check("delete project with correct password", client.request(
    "DELETE", f"/projects/{copy_project_id}", json={"manager_id": admin_id, "code": "1234"}
))
r = check("deleted project no longer listed", client.get("/projects"))
assert len(r.json()) == 1, r.json()

# --- re-running migrations on an already-migrated DB should be a no-op ---
dbmod.init_db()
r = check("managers survive re-migration", client.get("/managers"))
assert len(r.json()) == 2
r = check("projects survive re-migration", client.get("/projects"))
assert len(r.json()) == 1

# --- unit tests: language-code granularity bridging (resolve_lang_code /
# merge_lang_codes) ---
from app.excel_multi import resolve_lang_code, merge_lang_codes

# exact match wins even when a base-subtag match would also be possible
assert resolve_lang_code("ko-kr", ["ko-kr", "ko"]) == "ko-kr"
# plain code resolves to the one region-qualified entry that shares its base
assert resolve_lang_code("ko", ["ko-KR", "en-us"]) == "ko-KR"
# ...and the reverse direction: a region-qualified request resolves to a
# plain entry sharing its base
assert resolve_lang_code("ko-kr", ["ko", "en-us"]) == "ko"
# several distinct regions for the same base -> refuses to guess
assert resolve_lang_code("es", ["es-ES", "es-AR", "es-MX"]) is None
# unknown language entirely -> no match
assert resolve_lang_code("de", ["ko-KR", "es-MX"]) is None
# several same-base candidates, but with `values` supplied: if they all
# carry the identical rule anyway (Argentina and "rest of Latin America"
# sometimes do), it's safe to resolve rather than refuse — this is the
# real case Александр described: es-MX/es-CL/es-PE-style codes that all
# mean the same "not Spain, not Argentina" format
latam_values = {"es-ar": {"decimal": "—.50"}, "es-mx": {"decimal": "—.50"}}
assert resolve_lang_code("es", ["es-ar", "es-mx"], values=latam_values) == "es-ar"  # identical values -> safe to resolve either way
distinct_values = {"es-es": {"decimal": "—,50"}, "es-ar": {"decimal": "—.50"}}
assert resolve_lang_code("es", ["es-es", "es-ar"], values=distinct_values) is None  # genuinely different -> still refuses
# country-code-style label (Tone's real doc names Kazakh "KZ", Bengali
# "BD", Tajik "TJ" — the COUNTRY, not the ISO language subtag) bridges to
# the real language-region code via the region half, not the base half
assert resolve_lang_code("kz", ["kk-KR", "kk-KZ", "en-US"]) == "kk-KZ"
assert resolve_lang_code("bd", ["bn-BD", "hi-IN"]) == "bn-BD"
print("[OK] resolve_lang_code: exact match, any-subtag fallback (base or region, both "
      "directions), ambiguous multi-region refusal, unknown language, value-equality fallback")

assert merge_lang_codes(["ko", "ko-KR"]) == ["ko-KR"]
assert merge_lang_codes(["es", "es-ES", "es-AR", "es-MX"]) == ["es-AR", "es-ES", "es-MX"]
assert merge_lang_codes(["ru", "fr"]) == ["fr", "ru"]
# the same country-code-style bridging as above, but for the display list
assert merge_lang_codes(["kz", "kk-KZ"]) == ["kk-KZ"]
print("[OK] merge_lang_codes: collapses same-granularity duplicates, "
      "keeps genuinely distinct regional variants, bridges country-code-style "
      "labels to their region subtag, leaves unrelated codes alone")

# --- Александр hit this live: Tone.xlsx has both a bare "AR" (Arabic)
# column and an "ES (AR)" (Spanish, Argentina) column. Arabic's own bare
# code purely coincidentally spells the same two letters as Argentina's
# ISO-3166 country code, which sits inside "es-ar" as its region half —
# the OLD any-subtag matching treated that coincidence as "same language",
# silently merged bare "ar" into the Spanish group, and Arabic vanished
# from the known-languages list entirely. Same landmine, still latent,
# for five more real languages whose code coincidentally spells another
# country's real ISO-3166 code: Bengali/Brunei, Kyrgyz/Cayman Islands,
# Marathi/Mauritania, Tajik/Togo, Tagalog/Timor-Leste. ---
assert "ar" in merge_lang_codes(["ar", "es-ar", "es-mx"])
assert "es-ar" in merge_lang_codes(["ar", "es-ar", "es-mx"])
assert resolve_lang_code("ar", ["es-ar", "es-mx"]) is None  # Arabic must not resolve to a Spanish-Argentina row
for lang, other_country_code in [("bn", "bn"), ("ky", "ky"), ("mr", "mr"), ("tg", "tg"), ("tl", "tl")]:
    merged = merge_lang_codes([lang, f"fr-{other_country_code}", "fr-fr"])
    assert lang in merged, (lang, merged)
    assert f"fr-{other_country_code}" in merged, (lang, merged)
# but a bare code that ISN'T itself a real language (the agency's own
# country-code-style shorthand, same as the KZ/BD tests above) still
# bridges via region exactly as before — this fix must not have broken
# the legitimate case it was carved out of.
assert merge_lang_codes(["kg", "ky-KG"]) == ["ky-KG"]
assert resolve_lang_code("kg", ["ky-KG", "en-US"]) == "ky-KG"
# and a bare code that genuinely IS the same language as a region-qualified
# one (not a coincidence) still merges normally.
assert merge_lang_codes(["ar", "ar-eg"]) == ["ar-eg"]
# same coincidence, a sixth real case: Александр's files use bare "my" for
# Malay specifically (not ISO-639's own Burmese meaning — see
# claude_client.LANG_CODE_MEANING_OVERRIDES), which is itself also
# Malaysia's real ISO-3166 country code — bare "my" must bridge to a
# genuine "my-*" variant of Malay, but never get pulled into an unrelated
# language's Malaysia-region entry (e.g. "zh-MY") just because they
# happen to share those two letters.
assert merge_lang_codes(["my", "zh-MY", "zh-CN"]) == ["my", "zh-CN", "zh-MY"]
assert resolve_lang_code("my", ["zh-MY", "zh-CN"]) is None
print("[OK] merge_lang_codes/resolve_lang_code: a real independent language "
      "whose code coincidentally spells another country's ISO-3166 code "
      "(Arabic \"ar\" vs Argentina inside \"es-ar\", and the same latent risk "
      "for Bengali, Kyrgyz, Marathi, Tajik, Tagalog) is never absorbed into "
      "or resolved against an unrelated language's region — while the "
      "legitimate country-code-shorthand bridging (KZ/BD/KG-style) and "
      "genuine same-language bridging (ar/ar-eg) both still work")

# --- _lang_selected: decides whether a manager's target_langs_filter (raw
# catalog-checkbox codes) covers a given FILE column — used by the actual
# check run, unlike resolve_lang_code's own base-language shortcut (which
# is fine for merge_lang_codes' display grouping) it must NEVER bridge two
# codes that are BOTH already region-qualified just because they share a
# base language: "es-mx" ticked must never silently also check an "es-es"
# file column, since those are deliberately distinct, explicitly-added
# catalog languages. It still bridges a bare code against a region-
# qualified one either direction (a ticked bare "pt" covers a file's
# "pt-br" column, and vice versa), including the country-code-shorthand
# case (ticked "kz" covers a file's "kk-KZ" column) — exactly the
# Portuguese-default scenario this was added for — and refuses instead of
# guessing when more than one filter entry would bridge. ---
from app.excel_multi import _lang_selected

assert _lang_selected("ru", {"ru", "es-mx"}) is True  # exact match
assert _lang_selected("es-mx", {"ru", "es-mx"}) is True  # exact match
assert _lang_selected("pt-br", {"pt"}) is True  # bare filter covers a region-qualified file column
assert _lang_selected("pt", {"pt-br"}) is True  # and the reverse direction
assert _lang_selected("kk-KZ", {"kz"}) is True  # country-code-shorthand bridging, same as resolve_lang_code
assert _lang_selected("es-es", {"ru", "es-mx"}) is False  # NEVER bridge two distinct explicit regions
assert _lang_selected("ar", {"es-ar"}) is False  # Arabic must never match Argentina's region code
assert _lang_selected("es", {"es-ar", "es-mx"}) is False  # ambiguous bridge -> refuse, don't guess
# ...but NOT the other way around, deliberately: a bare catalog tick ("es")
# against a file with BOTH "es-ar" and "es-mx" columns selects each one
# independently rather than refusing both — over-including a language is a
# far smaller problem than the silent-drop bug this function exists to fix.
assert _lang_selected("es-ar", {"es"}) is True
assert _lang_selected("es-mx", {"es"}) is True
print("[OK] _lang_selected: bridges a bare catalog code against a file's region-qualified "
      "column (and vice versa), same as resolve_lang_code's bare/region bridging, but never "
      "bridges two codes that are both already region-qualified, refuses when a file column "
      "could ambiguously bridge to several ticked filter entries, and (deliberately, "
      "asymmetrically) still selects every explicit file column that bridges to one bare "
      "catalog tick rather than refusing to guess in that direction")

# --- Александр's Portuguese is always Brazilian, never Portugal's — a bare
# "PT" column (or a manually-typed catalog addition of just "pt") must
# default to "pt-br", the same way "ES (AR)" already tells the AI check it's
# Argentine Spanish rather than leaving a bare "es" to be guessed at. An
# EXPLICIT region must never be overridden by that default, whichever style
# it's written in. ---
from app.excel_multi import _normalize_lang_label

assert _normalize_lang_label("PT") == "pt-br"
assert _normalize_lang_label("pt") == "pt-br"
assert _normalize_lang_label("PT (PT)") == "pt-pt"  # explicit region always wins over the default
assert _normalize_lang_label("PT-BR") == "pt-br"  # already-explicit form passes through unchanged
assert _normalize_lang_label("PT (BR)") == "pt-br"
assert _normalize_lang_label("ES") == "es"  # the default is Portuguese-specific, not applied to other languages
print("[OK] _normalize_lang_label: a bare \"pt\"/\"PT\" defaults to Brazilian Portuguese "
      "(\"pt-br\"), while any explicitly-written region is always left alone")

# --- _label_to_code: the single choke point behind the manager-built global
# alias dictionary (see /language-aliases below) — a taught spelling always
# wins over whatever _normalize_lang_label's regex rules would have guessed,
# including for something that wouldn't even look language-shaped at all
# (contains a space, isn't short) — that's the whole point of teaching it.
# Falls back to the regular rules when nothing is taught. ---
from app.excel_multi import _label_to_code

geo_alias_map = {"geo": "ka", "portuguese brazil": "pt-br", "prbr": "pt-br"}
assert _label_to_code("GEO", geo_alias_map) == "ka"  # case-insensitive match on the taught alias
assert _label_to_code(" Geo ", geo_alias_map) == "ka"  # surrounding whitespace ignored
assert _label_to_code("Portuguese Brazil", geo_alias_map) == "pt-br"  # rescues something with a space, no regex could ever match this
assert _label_to_code("PRBR", geo_alias_map) == "pt-br"
assert _label_to_code("ES (MX)", geo_alias_map) == "es-mx"  # nothing taught for this -> falls back to the regular rules
assert _label_to_code("ES (MX)", None) == "es-mx"  # no alias map at all -> same fallback, no crash
print("[OK] _label_to_code: a taught alias (case-insensitive, whitespace-trimmed) always wins, "
      "including rescuing a spelling that wouldn't otherwise look language-shaped at all, and "
      "falls back to the regular _normalize_lang_label rules for anything not taught")

# --- pick_source_lang must bridge the same granularity mismatches as
# everything else in this file (via resolve_lang_code), not do a literal
# string match — Александр hit this live: he picked "Русский" as the
# source language, but his file's Russian column wasn't spelled exactly
# "ru" (a region-qualified "ru-RU"), the old literal check silently missed
# it, and the source language silently fell back to whatever column was
# labeled "en-001" instead — his check ran source-vs-source against the
# wrong pair of columns without any error or warning. ---
from app.excel_multi import pick_source_lang

fake_sheets = [{"languages": ["ru-RU", "en-001", "kz"]}]
assert pick_source_lang(fake_sheets, "ru") == "ru-RU", pick_source_lang(fake_sheets, "ru")
assert pick_source_lang(fake_sheets, "kk-KZ") == "kz", pick_source_lang(fake_sheets, "kk-KZ")
# still falls back to English, then alphabetically first, when the
# requested language genuinely isn't in the file at all
assert pick_source_lang(fake_sheets, "de") == "en-001", pick_source_lang(fake_sheets, "de")
assert pick_source_lang([{"languages": ["kz", "en-001"]}], None) == "en-001"
print("[OK] pick_source_lang: resolves the manager's chosen source language against "
      "a differently-granular spelling of the same language in the file, instead of "
      "silently falling back to English")

# --- model tiering: confirmed "hard" languages get the stronger model,
# matched by base subtag so any region variant of them qualifies too.
# List replaced wholesale 2026-09-18 (Александр's ask, after comparing
# real Opus vs Sonnet reports): kk/uz/sw/az moved OFF the hard list (they
# now get CLAUDE_MODEL like everything else), replaced by a new set of
# 15 languages including the non-ISO "hing" (Hinglish) code. ---
from app.claude_client import _model_for_lang
from app.config import settings

for hard in [
    "ar", "ar-SA", "bn", "bn-BD", "el", "el-GR", "hi", "hi-IN", "hing", "id", "id-ID",
    "ky-KG", "ko", "ko-KR", "mr-IN", "ms", "ms-MY", "ro", "ro-RO", "te-IN", "th", "th-TH",
    "tg-TJ", "ur", "ur-PK",
]:
    assert _model_for_lang(hard) == settings.CLAUDE_MODEL_HARD, hard
for normal in ["ru", "es-mx", "en", "de-DE", "fr", "kk", "kk-KZ", "uz", "sw-KE", "az-AZ"]:
    assert _model_for_lang(normal) == settings.CLAUDE_MODEL, normal
print("[OK] _model_for_lang: confirmed the current hard-language list (ar/bn/el/hi/hing/id/ky/ko/mr/ms/"
      "ro/te/th/tg/ur) routes to CLAUDE_MODEL_HARD by base subtag, and that kk/uz/sw/az — on the OLD list "
      "— now route to CLAUDE_MODEL like every other 'normal' language")

# --- MODEL_PRICING_PER_TOKEN / _usage_cost: Александр's real bug
# (2026-09-17) — CLAUDE_MODEL on Railway had already moved on to
# "claude-sonnet-5", but this table only listed the two older model ids,
# so _usage_cost's deliberate "can't price it, show $0 rather than guess"
# fallback (see its own docstring) kicked in for EVERY real check, and a
# check that actually cost roughly $0.50 showed "Стоимость: 0 $" instead.
# Added "claude-sonnet-5" and "claude-opus-5" at their own current prices
# (confirmed live against platform.claude.com/docs/en/about-claude/pricing
# the same day) — this locks in that a check billed under either of those
# model ids now gets a real, non-zero cost instead of silently zeroing
# out, while an actually-unlisted model id still safely falls back to 0.0
# rather than guessing at a stale price. ---
from app.claude_client import _usage_cost, MODEL_PRICING_PER_TOKEN

assert "claude-sonnet-5" in MODEL_PRICING_PER_TOKEN and "claude-opus-5" in MODEL_PRICING_PER_TOKEN
sonnet5_cost = _usage_cost("claude-sonnet-5", {"input_tokens": 1_000_000, "output_tokens": 1_000_000})
assert abs(sonnet5_cost - 12.00) < 1e-9, sonnet5_cost  # $2 in + $10 out per Mtok
opus5_cost = _usage_cost("claude-opus-5", {"input_tokens": 1_000_000, "output_tokens": 1_000_000})
assert abs(opus5_cost - 30.00) < 1e-9, opus5_cost  # $5 in + $25 out per Mtok
# the batch (Message Batches API) discount still applies to these too
sonnet5_batch_cost = _usage_cost("claude-sonnet-5", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}, batch=True)
assert abs(sonnet5_batch_cost - 6.00) < 1e-9, sonnet5_batch_cost
# an actually-unpriced model id still safely falls back to 0.0 rather than
# guessing at a stale/wrong price — the exact safe behavior that made this
# bug visible as "$0" instead of a silently wrong non-zero number
assert _usage_cost("claude-some-future-model", {"input_tokens": 1000, "output_tokens": 1000}) == 0.0
print("[OK] MODEL_PRICING_PER_TOKEN now also covers claude-sonnet-5 and claude-opus-5 at their own "
      "current prices, so a check billed under either no longer silently shows \"Стоимость: 0 $\" while "
      "actually costing real money — while a genuinely unpriced/unknown model id still safely falls back "
      "to $0 rather than guessing at a stale price")

# --- "my" is ISO-639's code for Burmese, but Александр's files use it for
# Malay — left unclarified, the model assumes Burmese and reports correct
# Malay text as being in the wrong language. The prompt must explicitly
# override this one code's meaning instead of just stating the raw code. ---
from app.claude_client import _target_lang_line

my_line = _target_lang_line("my")
assert "малайск" in my_line.lower(), my_line
assert "бирманск" in my_line.lower(), my_line  # explicitly rules out the ISO-standard meaning
normal_line = _target_lang_line("ru")
assert "малайск" not in normal_line.lower() and "бирманск" not in normal_line.lower(), normal_line
print("[OK] _target_lang_line: the «my» code is explicitly clarified as Malay (not the ISO-standard "
      "Burmese) so the model doesn't misjudge correct Malay text as the wrong language")

# --- numbers check: a correctly localized decimal comma or zero-padded
# hour must NOT be flagged as a mismatch — Александр hit this live: an
# Azerbaijani translation writing "0,40" for the source's "$0.40" and
# "03:00" for the source's "3:00" was flagged "numbers" even though
# nothing was actually mistranslated, just correctly reformatted. ---
from app.rule_checks import check_numbers

az_source = (
    "To participate in the tournament, you must confirm participation in any game from the list, "
    "place bets from $0.40, and complete missions. The tournament runs from 09/22/2026 (3:00 UTC) "
    "to 09/28/2026 (22:59 UTC). The full rules are available in the in-game menu. Minimum bet: $0.40"
)
az_translation = (
    "Turnirdə iştirak etmək üçün siyahıdakı istənilən oyunda iştirakınızı təsdiqləməli, 0,40 ₼ və "
    "daha çox mərc etməli və missiyaları yerinə yetirməlisiniz. Turnir from 22/09/2026 (03:00 UTC) – "
    "28/09/2026 (22:59 UTC) tarixləri arasında keçirilir. Tam qaydaları oyun içi menyuda oxuya "
    "bilərsiniz. Min. mərc: 0,40 ₼"
)
assert check_numbers(az_source, az_translation) == [], check_numbers(az_source, az_translation)
# A genuine mismatch (thousands grouping aside — see below) must still fire.
assert check_numbers("The bonus is $50.", "Бонус составляет $500.") != []
# A comma used to GROUP thousands (not as a decimal separator) is left
# alone — "50,000" really is a different number from "50" if unmatched.
assert check_numbers("$50,000 prize", "50 тысяч приз") != []
print("[OK] check_numbers: decimal-comma and zero-padded-hour localization no longer "
      "false-flagged as a numbers mismatch; real mismatches and thousands-grouping still caught")

# --- Indian numbering (lakh/crore): a rupee amount grouped "1,00,000" style
# (2-digit groups, not the Western 3-digit "100,000") is the same value,
# not a mismatch — Александр's real complaint (2026-09-17), where an
# Indian-language column correctly localized a source amount that showed
# up "wrong" purely because the grouping shape itself wasn't recognized. ---
assert check_numbers("Prize: ₹100,000", "Prize: ₹1,00,000") == []
assert check_numbers("1 000 000₹ prize", "₹10,00,000 prize") == []
assert check_numbers("₹1,23,45,678 prize", "₹12345678 prize") == []  # crore-scale, ungrouped translation
assert check_numbers("₹100,000", "₹1,50,000") != []  # genuinely different amount must still be caught
print("[OK] check_numbers: Indian-style lakh/crore digit grouping (e.g. ₹1,00,000 for ₹100,000) is "
      "recognized as the same grouping shape, not a different number, while an actually different "
      "rupee amount is still caught")

# --- short day.month dates with NO year at all ("20.09" vs "09/20") —
# Александр's second date complaint (2026-09-17): the existing multiset
# comparison only decomposed a date with 2+ separators (day.month.year);
# a bare 2-part dotted date fell through and was compared as one literal
# string, so a correctly reordered/reformatted short date without a year
# was wrongly flagged as a numbers mismatch. An ordinary decimal ("$0.40")
# must stay completely unaffected. ---
assert check_numbers("Confirm by 09/20, 23:59", "Подтверди до 20.09, 23:59") == []
assert check_numbers("Confirm by 09/21, 23:59", "Подтверди до 20.09, 23:59") != []  # genuinely different day
assert check_numbers("Minimum bet: $0.40", "Мин. ставка: $0.40") == []
assert check_numbers("Minimum bet: $0.40", "Мин. ставка: $0.44") != []  # genuine decimal difference
print("[OK] check_numbers: a short day.month date with no year (\"20.09\" vs \"09/20\") can be freely "
      "reordered/reformatted between languages without being flagged, while an actually different day "
      "and an ordinary decimal value (\"$0.40\") are both still handled correctly")

# --- the short-date fix above is genuinely ambiguous with an ordinary
# 2-decimal-place price (a caught-in-review risk, 2026-09-17): "$20.09"
# transposed to "$9.20" is exactly as shaped as a reordered date. Fixed by
# context instead of shape — a currency symbol/code close to the number
# suppresses the date interpretation, so a transposed price is still an
# exact-string mismatch and gets caught, while the date fix above (nothing
# currency-shaped nearby) is untouched. ---
assert check_numbers("Bonus: $20.09", "Bonus: $9.20") != [], "a transposed price must still be caught"
assert check_numbers("Min deposit $10.15", "Min deposit $15.10") != []
assert check_numbers("Cashback: 1500 USDT", "Кэшбэк: 1500 USDT") == []  # unaffected sanity check
print("[OK] check_numbers: the short-date fix doesn't come at the cost of missing a genuinely transposed "
      "price — a number sitting next to a currency symbol or code keeps its exact digit order compared, "
      "even though it's shaped just like a reorderable date")

# --- emoji: presence/absence (folded into check_punctuation, same as
# check_mixed_script — no separate checkbox) and spacing around an emoji.
# Александр's ask (2026-09-17): the platform wasn't reliably catching a
# missing emoji, or an emoji glued to surrounding text without a space. ---
from app.rule_checks import check_emoji, check_punctuation

emoji_src = "Halloween Lootbox 🔥\n\nResults sent to this bot 🫶"
assert check_emoji(emoji_src, "Хэллоуин Lootbox 🔥\n\nРезультаты придут в бот 🫶") == []
missing_emoji = check_emoji(emoji_src, "Хэллоуин Lootbox\n\nРезультаты придут в бот")
assert missing_emoji and missing_emoji[0]["type"] == "emoji" and "🔥" in missing_emoji[0]["message"] \
    and "🫶" in missing_emoji[0]["message"], missing_emoji
unspaced = check_emoji(emoji_src, "Хэллоуин Lootbox🔥\n\nРезультаты придут в бот 🫶")
assert any("отделён" in f["message"] for f in unspaced) and "🔥" in unspaced[-1]["message"], unspaced
assert check_emoji("I ❤️ this", "Мне это ❤️ нравится") == []  # variation-selector heart survives intact
# check_punctuation must pick up an emoji finding on its own (not just
# piggyback on an unrelated mixed-script hit) — a purely-emoji case with
# no other punctuation/script issue at all proves the wiring itself.
assert any(f["type"] == "emoji" for f in check_punctuation("Go 🔥", "Иди")), \
    "check_punctuation must include emoji findings automatically, no separate checkbox needed"
print("[OK] check_emoji: a missing/extra emoji between source and translation is caught (folded into "
      "\"Оформление\" automatically, same as check_mixed_script), and an emoji glued to surrounding text "
      "without a space is flagged separately from presence/absence")

# --- three spacing false positives caught in review (2026-09-17), all
# fixed: (1) a flag emoji (two regional-indicator code points, no ZWJ
# between them) must never look "unspaced from itself"; (2) two or more
# DIFFERENT emoji clustered together with no space between them ("🎉🔥💰")
# is completely normal promo style, not a spacing mistake; (3) ordinary
# punctuation hugging an emoji on either side ("Поздравляем!🎉",
# "(🔥 предложение)") is fine — only an actual letter/digit glued directly
# to the emoji is the real problem. ---
assert check_emoji("Welcome to Brazil 🇧🇷!", "Добро пожаловать в Бразилию 🇧🇷!") == []
assert check_emoji("Win big 🎉🔥💰", "Крупный выигрыш 🎉🔥💰") == []
assert check_emoji("Congrats!🎉", "Поздравляем!🎉") == []
assert check_emoji("(🔥 hot deal)", "(🔥 горячее предложение)") == []
# ...while a letter genuinely touching the emoji is still caught.
assert check_emoji("Lootbox 🔥 event", "Lootbox🔥 событие") != []
print("[OK] check_emoji spacing: a flag (two joined regional-indicator letters), a cluster of several "
      "different emoji together, and an emoji hugging ordinary punctuation on either side are all left "
      "alone — only an emoji glued directly to a letter/digit is flagged")

# --- completeness vs untranslatable: an English brand/term/event name left
# untranslated in a Russian source (_source_lang_note's own special rule)
# must not tell the model two contradictory things when both checks are
# selected together (the common case, both default on) — defer to the
# more specific "непереводимые термины" category when it's part of the
# run, falling back to "неполнота перевода" only when it isn't. ---
from app.claude_client import _source_lang_note

assert "непереводимые термины" in _source_lang_note("ru", ["untranslatable", "completeness"])
assert "неполнота перевода" not in _source_lang_note("ru", ["untranslatable", "completeness"])
assert "неполнота перевода" in _source_lang_note("ru", ["completeness"])
assert "непереводимые термины" not in _source_lang_note("ru", ["completeness"])
assert _source_lang_note("ru", None) != "" and "неполнота перевода" in _source_lang_note("ru", None)
assert _source_lang_note("en", ["untranslatable"]) == ""  # only applies to a Russian source at all
print("[OK] _source_lang_note: an English term left untranslated in a Russian source is filed under "
      "«непереводимые термины» when that check is selected (avoiding a contradiction with its own "
      "\"unchanged is correct\" rule), and only falls back to «неполнота перевода» when it isn't")

# --- placeholders check: a literal "\u00A0" escape token (some of
# Александр's Crowdin exports write a non-breaking space out this way,
# rather than as the actual invisible character) must be recognized as a
# placeholder just like {name}/%s/<tag> — Александр hit this live: "Earn
# points in tournament games and\u00A0win cash prizes" lost the "\u00A0" in
# translation and check_placeholders never noticed, since none of the
# existing placeholder styles matched a bare backslash+u+4-hex-digit run. ---
from app.rule_checks import check_placeholders

nbsp_source = "Earn points in tournament games and\\u00A0win cash prizes"
nbsp_translation_missing = "Заработайте очки в турнирных играх и выиграйте денежные призы"
nbsp_translation_kept = "Заработайте очки в турнирных играх и\\u00A0выиграйте денежные призы"
assert check_placeholders(nbsp_source, nbsp_translation_missing) != [], "must catch a dropped \\u00A0"
assert check_placeholders(nbsp_source, nbsp_translation_kept) == [], "must not flag when \\u00A0 survives"
# still recognizes every previously-supported style alongside the new one
assert check_placeholders("Hello {name}, you have %d points", "Привет {name}, у вас %d очков") == []
assert check_placeholders("Hello {name}", "Привет") != []  # {name} genuinely dropped
print("[OK] check_placeholders: a literal \"\\u00A0\" escape token (and any other \\uXXXX-style escape) "
      "is now recognized as a placeholder just like {name}/%s/<tag>, alongside every style already "
      "supported before")

# --- letter-run placeholder protection (e.g. "XXXXXXXX", "YYYY" standing in
# for masked/dynamic data): the letter count must survive translation
# exactly, and the alphabet (Latin vs Cyrillic) must never silently swap —
# both invisible-to-the-eye mistakes, same spirit as check_mixed_script
# below. Александр's ask, 2026-09-17. ---
from app.rule_checks import check_letter_placeholders

# unchanged run — no finding
assert check_letter_placeholders("Card: XXXXXXXX", "Карта: XXXXXXXX") == []
# letter count changed (one X dropped) — must be caught, and the message
# must show both the original and the (wrong) new length
count_result = check_letter_placeholders("Card: XXXXXXXX", "Карта: XXXXXXX")
assert count_result != [] and count_result[0]["type"] == "placeholders" and count_result[0]["severity"] == "high"
assert "XXXXXXXX" in count_result[0]["message"] and "XXXXXXX" in count_result[0]["message"]
# alphabet swapped to a look-alike Cyrillic letter, same length — must be
# caught even though "ХХХХХХХХ" looks pixel-identical to "XXXXXXXX"
script_result = check_letter_placeholders("Card: XXXXXXXX", "Карта: ХХХХХХХХ")
assert script_result != [] and "алфавитом" in script_result[0]["message"]
# the placeholder disappearing entirely from the translation
assert check_letter_placeholders("Year: YYYY", "Год: 2026") != []
# a genuinely different letter/length in the translation that's actually a
# SECOND, unrelated placeholder later in a longer source is still paired up
# positionally and checked independently
assert check_letter_placeholders("XXXX and YYYY", "XXXX и YYYY") == []
# two bugs caught in review of the first version and fixed:
# (1) a translator reordering the clauses (completely normal in Russian)
# must NOT make two untouched, merely-swapped placeholders look "changed"
assert check_letter_placeholders("Card XXXXXXXX, Year YYYY", "Год YYYY, Карта XXXXXXXX") == [], \
    "reordered-but-unchanged placeholders must never be cross-matched and flagged"
# (2) one placeholder genuinely dropped from the middle of several must be
# reported as itself missing, not misattributed to a different, unrelated
# placeholder that's actually still present
dropped_result = check_letter_placeholders("XXXX and YYYY", "YYYY")
assert dropped_result != [] and "XXXX" in dropped_result[0]["message"] and "потерялась" in dropped_result[0]["message"], dropped_result
assert not any("YYYY" in f["message"] and "потерялась" in f["message"] for f in dropped_result), (
    "YYYY is still present (just reordered to the front) and must never be reported as missing", dropped_result
)
# (1b) the mirror bug: a source with NO letter-run placeholder at all used
# to short-circuit before the translation was even inspected, so a run
# appearing only in the (garbled) translation went completely unreported
assert check_letter_placeholders("Special offer today", "Специальное предложение ХХХХХХХХ") != [], (
    "an extra letter-run placeholder appearing only in the translation must be caught even when the "
    "source has no letter-run placeholder of its own"
)
# a short, coincidental repeated-letter token that's NOT a placeholder
# (a roman numeral, a batteries size, a URL prefix, a Russian company
# abbreviation) must never be mistaken for one — none of these reach the
# minimum run length of 4
assert check_letter_placeholders("Buy III tickets now, see www.site.com, need 2 AA batteries, ООО Ромашка", "Купите III билетов, см. www.site.com, нужны 2 AA батарейки, ООО Ромашка") == []
# folded into run_rule_checks under the same "placeholders" checkbox as the
# existing tag/placeholder check — no new checkbox needed on the frontend
from app.rule_checks import run_rule_checks
folded = run_rule_checks("Card: XXXXXXXX", "Карта: XXXXXXX", checks=["placeholders"])
assert any(f["type"] == "placeholders" for f in folded), folded
print("[OK] check_letter_placeholders: a letter-run placeholder (\"XXXXXXXX\", \"YYYY\") losing or gaining "
      "letters, or silently swapping to a look-alike letter from the other alphabet (Cyrillic \"Х\" for "
      "Latin \"X\"), is caught under the existing \"placeholders\" checkbox — while an ordinary short "
      "repeated-letter token that isn't really a placeholder (roman numeral, battery size, URL, company "
      "abbreviation) is correctly left alone")

# --- mixed-script (homoglyph) detection, folded into check_punctuation: a
# word mixing a look-alike Cyrillic letter into an otherwise-Latin word (or
# vice versa) is invisible to the eye but real in the text — Александр
# asked whether this could be caught algorithmically. No opt-in checkbox
# needed (folded straight into "Оформление", like check_numbers already
# is) since a genuinely mixed-script word has essentially no legitimate
# reason to exist in any of these languages. ---
from app.rule_checks import check_mixed_script, check_punctuation

assert check_mixed_script("Это обычный русский текст.") == []  # pure Cyrillic — fine
assert check_mixed_script("This is plain English text.") == []  # pure Latin — fine
assert check_mixed_script("Используйте Google Play для входа.") == []  # a pure-Latin brand NEXT TO Cyrillic words is fine
mixed_result = check_mixed_script("Используйте Gооgle Play для входа.")  # Cyrillic "оо" inside a Latin word
assert mixed_result != [] and "Gооgle" in mixed_result[0]["message"], mixed_result
assert mixed_result[0]["type"] == "punctuation"  # rides along under the same check type, no new checkbox
# check_punctuation (the actual dispatch point run_rule_checks calls) picks
# this up automatically, without any separate call needed.
assert any(f["type"] == "punctuation" for f in check_punctuation("Go.", "Идите в Gооgle Play.")), \
    "check_punctuation must include the mixed-script finding, not just check_mixed_script on its own"
print("[OK] check_mixed_script: a word mixing look-alike Cyrillic/Latin letters (e.g. Cyrillic \"о\" "
      "inside an otherwise-Latin word) is caught automatically under \"Оформление\" — a pure-script "
      "word, even a Latin brand name sitting next to Cyrillic text, is never flagged")

# --- SMS/GSM-7bit charset check: opt-in only (see CHECK_OPTIONS on the
# frontend — unticked by default), flags any character outside the strict
# Latin set Александр's SMS spec allows, so a Turkish "Günaydın" is caught
# (İ/diacritics aren't GSM-safe) while its transliterated "Gunaydin" is
# clean, and ordinary Cyrillic/CJK/etc. text is obviously NOT what this
# check is for (enabling it there would flag nearly every character — by
# design, since it's never on by default and must be deliberately ticked
# only for an actual SMS deliverable). ---
from app.rule_checks import check_sms_charset
from app import schemas
from app import main

assert check_sms_charset("Gunaydin! Win up to 100 USD, terms apply (18+).") == []  # fully GSM-safe
bad = check_sms_charset("G\u00fcnaydın! Ma\u00f1ana \u2018special\u2019 \u2014 win now.")
assert bad != [], "diacritics, curly quotes and an em dash must all be flagged"
assert bad[0]["type"] == "sms_charset"
assert "ü" in bad[0]["message"] or "\u00fc" in bad[0]["message"]
# not wired into any default check list — must be explicitly requested
assert "sms_charset" not in schemas.DEFAULT_CHECKS
assert "sms_charset" not in main.DEFAULT_MULTI_CHECKS
print("[OK] check_sms_charset: flags diacritics/typographic quotes/em dash/etc. against the strict "
      "GSM 7-bit Latin set Александр's SMS spec requires, stays silent on plain GSM-safe text, and is "
      "excluded from every default check list (opt-in only, never on unless explicitly ticked)")

# --- dropping (or keeping) a thousands-grouping comma must not be flagged
# either — Александр hit this live: a translation correctly kept some of a
# promo's big numbers grouped ("1,500,000") but wrote a smaller one
# ungrouped ("1400" for the source's "1,400"), and it was flagged as a
# mismatch even though the value never changed. ---
assert check_numbers("Win up to $1,400 today", "Выиграйте до $1400 сегодня") == []
assert check_numbers("Prize: $1,500,000", "Приз: $1500000") == []
# But an actually different grouped number must still be caught.
assert check_numbers("Win up to $1,400 today", "Выиграйте до $1,500 сегодня") != []
print("[OK] check_numbers: a thousands-grouping comma can be freely added or dropped "
      "without being flagged, while an actually different grouped number is still caught")

# --- a SPACE is just as legitimate a thousands separator as a comma —
# it's how Russian formats a big number ("1 500 000") — but NUMBER_RE
# can't include a bare space (that would merge unrelated numbers separated
# by ordinary whitespace). Александр hit this live in a real prize-table
# promo: every single big number came back "different" between Russian
# ("1 500 000") and English/Spanish ("1,500,000") for no reason at all. ---
assert check_numbers("1st place — 1,500,000 ARS", "1 место — 1 500 000 ARS") == []
assert check_numbers("Prize: 90,000 ARS", "Приз: 90 000 ARS") == []
# An actually different space-grouped number must still be caught, and the
# message must point at the specific numbers that differ, not dump every
# number in the text — a long paragraph can have 20+ numbers where only
# one is actually wrong.
mismatch = check_numbers("70th-99th place — 70,000 ARS", "С 70 по 99 место — 72 000 ARS")
assert mismatch, mismatch
assert "70000" in mismatch[0]["message"] and "72000" in mismatch[0]["message"], mismatch
print("[OK] check_numbers: a space used to group thousands (standard Russian formatting) "
      "is treated the same as a comma — freely interchangeable without being flagged — "
      "while an actually different number is still caught, and the message names the "
      "specific number(s) that differ rather than dumping the whole list")

# --- a PERIOD is just as legitimate a thousands separator as a comma or a
# space — German, Spanish and several other target languages group
# thousands that way ("50.000", "1.500.000"). Александр hit this live: the
# report showed source "50000" and translation "50.000" as two different
# numbers even though the value never changed, purely because the comma
# rule above only covered the comma. Also covers a currency symbol whose
# position and spacing changes between source and translation ("$100000"
# vs "100.000$") — the symbol itself was never part of the extracted
# number to begin with, so its placement was already a non-issue; this
# just confirms it stays that way once the number itself is also
# period-grouped. ---
assert check_numbers("Win up to 50000 today", "Выиграйте до 50.000 сегодня") == []
assert check_numbers("Prize: $1500000", "Приз: 1.500.000$") == []
assert check_numbers("Min. bet: $100000", "Мин. ставка: 100.000 $") == []
# But an actually different period-grouped number must still be caught.
mismatch_dot = check_numbers("Win up to 50000 today", "Выиграйте до 55.000 сегодня")
assert mismatch_dot, mismatch_dot
assert "50000" in mismatch_dot[0]["message"] and "55000" in mismatch_dot[0]["message"], mismatch_dot
print("[OK] check_numbers: a period used to group thousands (standard German/Spanish "
      "formatting) is treated the same as a comma or a space — freely interchangeable "
      "without being flagged, including when a currency symbol moves position/spacing "
      "along with it — while an actually different number is still caught")

# --- a DATE must not be flagged just because the target language writes
# the day/month/year in a different order and/or with a different
# separator — Александр hit this live: a source date "09/22/2026"
# (MM/DD/YYYY, slash-separated) was correctly localized as "22.09.2026"
# (DD.MM.YYYY, dot-separated), and the check flagged it as a numbers
# mismatch even though every digit was correct — only the grouping/order
# changed, which is expected and fine. ---
date_source = "The tournament runs from 09/22/2026 (3:00 UTC) to 09/28/2026 (22:59 UTC)."
date_translation_ru = "Турнир проходит с 22.09.2026 (03:00 UTC) по 28.09.2026 (22:59 UTC)."
assert check_numbers(date_source, date_translation_ru) == [], check_numbers(date_source, date_translation_ru)
# But an actually WRONG date (a real digit changed, not just reordered)
# must still be caught.
date_translation_wrong = "Турнир проходит с 23.09.2026 (03:00 UTC) по 28.09.2026 (22:59 UTC)."
assert check_numbers(date_source, date_translation_wrong) != []
print("[OK] check_numbers: a date's day/month/year order and separator style "
      "(dots vs slashes) can change freely between languages without being flagged, "
      "while an actual wrong date digit is still caught")

# --- the "typo" AI check catches a WRONG CURRENCY entirely (e.g. euro
# instead of dollar) as a genuine translation error, not just a stylistic
# quirk — Александр hit a real case where $0.40 was mistranslated as
# "0,40 €" ---
from app.claude_client import CHECK_LABELS

assert "ДРУГАЯ ВАЛЮТА" in CHECK_LABELS["typo"], CHECK_LABELS["typo"]
print("[OK] currency identity (wrong currency, e.g. € instead of $) is scoped to «опечатки/ошибки»")

# --- untranslatable: Александр's real feedback (2026-09-17) — a confusing
# self-contradicting finding ("'Grand Prix' shouldn't have been translated,
# but it correctly stayed 'Grand Prix' — error") shows the model needs an
# EXPLICIT instruction that leaving such a term untouched is always
# correct, never a finding to report on its own. Also broadens the
# category (events/promos, not just tournaments/games/brands/products) and
# gives the model a concrete cross-check: if the source itself already
# left the term untranslated, that's strong evidence it should stay
# untranslated everywhere. ---
assert "ПРАВИЛЬНО, находки быть не должно" in CHECK_LABELS["untranslatable"], CHECK_LABELS["untranslatable"]
assert "САМОМ ИСХОДНИКЕ" in CHECK_LABELS["untranslatable"], CHECK_LABELS["untranslatable"]
assert "акций" in CHECK_LABELS["untranslatable"], CHECK_LABELS["untranslatable"]
print("[OK] «непереводимые термины» now explicitly tells the model that a term left untouched in "
      "translation is always correct on its own (never a finding), and to treat the source itself "
      "already leaving a term untranslated as a signal it should stay that way in every language")

# --- untranslatable, take two: Александр's real feedback (2026-09-17,
# same day, a follow-up on the very same check) — a tournament name
# ("Elite & Fortune") fully respelled phonetically in a DIFFERENT alphabet
# ("엘리트 & 포춘" in Hangul) in 2 of 9 rows, while correctly kept in Latin
# in the other rows, went completely unflagged — because the rule above
# used to lump "транслитерация" (any transliteration at all) in with
# grammatical case endings as both universally fine. Asked directly, he
# wants full alphabet-to-alphabet transliteration of a name/brand ALWAYS
# flagged as an error — even where it's done consistently everywhere in
# the document — while a grammatical ending attached to a term that's
# otherwise kept in its OWN original spelling (e.g. Russian "Grand
# Prix'а") must stay exempt, or the original "Grand Prix" self-
# contradiction bug (above) would come right back. ---
assert "хангылем" in CHECK_LABELS["untranslatable"], CHECK_LABELS["untranslatable"]
assert "Гран При" in CHECK_LABELS["untranslatable"], CHECK_LABELS["untranslatable"]
assert "ВСЕГДА ошибка" in CHECK_LABELS["untranslatable"], CHECK_LABELS["untranslatable"]
assert "Grand Prix'а" in CHECK_LABELS["untranslatable"], CHECK_LABELS["untranslatable"]
# the old blanket exemption (ANY transliteration, no matter the alphabet,
# is fine) must be gone — that's exactly the bug being fixed here
assert "транслитерация и падежные/грамматические окончания" not in CHECK_LABELS["untranslatable"], (
    CHECK_LABELS["untranslatable"]
)
print("[OK] «непереводимые термины»: a name/brand fully respelled phonetically into a DIFFERENT alphabet "
      "(e.g. Latin -> Hangul/Cyrillic) is now always flagged, even if done consistently everywhere in the "
      "document — while a grammatical ending attached to a term left in its OWN original spelling stays "
      "exempt, so the original 'Grand Prix left correctly untouched' self-contradiction can't come back")

# --- completeness: Александр's real feedback (2026-09-17) — the check was
# missing cases where a whole sentence/chunk of the source was dropped
# from the translation entirely (not left in the source language, just
# GONE), while still correctly staying silent on natural, small stylistic
# omissions (a dropped article/filler word). The label now names this
# third case explicitly and draws the small-vs-large line in words. ---
assert "ПРОПУЩЕН из перевода целиком" in CHECK_LABELS["completeness"], CHECK_LABELS["completeness"]
assert "естественное опущение одного-двух слов" in CHECK_LABELS["completeness"], CHECK_LABELS["completeness"]
assert "КУСОК СМЫСЛА" in CHECK_LABELS["completeness"], CHECK_LABELS["completeness"]
print("[OK] «неполнота перевода» now explicitly covers a whole sentence/chunk being dropped from the "
      "translation entirely (not just left-over source-language text), while still distinguishing that "
      "from a small, natural stylistic omission that isn't a finding")

# --- completeness: Александр's ask (2026-09-17) — a dropped/malformed
# call-to-action arrow ("->", "→", "=>", used in mailings as a link/button
# cue) should also be caught, but deliberately by the AI/prompt rather than
# a hardcoded rule check, since he wasn't confident every real-world variant
# could be enumerated in an algorithm. ---
assert "->" in CHECK_LABELS["completeness"], CHECK_LABELS["completeness"]
assert "призыва к действию" in CHECK_LABELS["completeness"], CHECK_LABELS["completeness"]
assert "искажён" in CHECK_LABELS["completeness"], CHECK_LABELS["completeness"]
print("[OK] «неполнота перевода» now also asks the AI to flag a call-to-action arrow (\"->\" and similar) "
      "that's dropped or altered between source and translation, per Александр's explicit ask that this "
      "stay AI-judged rather than a hardcoded rule (the forms vary too much to enumerate reliably)")

# --- a plain misspelling in the translation itself ("resulits" for
# "results") must be in scope too, even though the meaning is still
# perfectly clear from context — Александр hit this live: the AI didn't
# report it because the old wording scoped "опечатки/ошибки" to ONLY
# meaning-distorting errors, which technically excludes an obvious
# spelling slip a reader can still understand. The label now explicitly
# names plain misspellings as their own always-in-scope category,
# distinct from a stylistic/synonym choice (which stays out of scope). ---
assert "неправильно написанное слово" in CHECK_LABELS["typo"], CHECK_LABELS["typo"]
print("[OK] a plain spelling mistake is explicitly in scope for «опечатки/ошибки» even when the "
      "meaning is still clear from context, distinct from a stylistic/synonym choice which isn't")

# --- but when the free "numbers" rule check is ALSO running in the same
# request, the AI must not also report a plain digit/date mismatch under
# "typo" — that's the exact duplicate Александр hit: the same wrong-year
# date shown once as "numbers" and again, reworded, as "typo". The AI is
# told to leave plain digits to the rule check and only still flag
# currency IDENTITY (not itself a digit) under "typo". When "numbers"
# ISN'T part of this run, the AI keeps acting as the sole backstop for a
# wrong number, exactly as before this fix. ---
from app.claude_client import _calibration

calib_with_numbers = _calibration(["typo", "numbers"])
calib_without_numbers = _calibration(["typo"])
assert "не сообщай о них здесь" in calib_with_numbers, calib_with_numbers
assert "не сообщай о них здесь" not in calib_without_numbers, calib_without_numbers
assert "валюта или число не совпадают" in calib_without_numbers, calib_without_numbers
print("[OK] the AI is told to skip plain digit/date mismatches under «опечатки/ошибки» whenever "
      "the free «numbers» rule check is also part of the same run (avoids the same error being "
      "reported twice under two different labels), and stays the sole backstop for numbers "
      "otherwise")

# --- Александр's team's real observation (2026-09-17): the AI seemed to
# stop checking a pair once it found ONE problem, even though a real pair
# can have several distinct issues at once. CALIBRATION_BASE (folded into
# both SINGLE_PROMPT and BATCH_PROMPT via {calibration}) now explicitly
# says a pair can hold several different problems and the model must check
# EVERY selected criterion rather than stopping after the first hit. ---
from app.claude_client import CALIBRATION_BASE

assert "НЕСКОЛЬКО разных проблем" in CALIBRATION_BASE, CALIBRATION_BASE
assert "не останавливайся после первой" in CALIBRATION_BASE, CALIBRATION_BASE
print("[OK] the calibration text explicitly tells the model a single pair can hold several distinct "
      "problems at once and it must keep checking every selected criterion instead of stopping after "
      "the first finding in that pair")

# --- Александр's ask, 2026-09-17: a single Excel cell can hold a long,
# multi-sentence paragraph, with the SAME problem occurring in several
# different sentences of that one cell — that must come back as one
# separate finding PER occurrence, not one blended finding for the whole
# cell, and not silently deduplicated the way a repeat ACROSS rows is
# (that's a completely different mechanism — see BATCH_PROMPT's "rows"
# tests below). ---
assert "повторов ВНУТРИ одной и той же пары" in CALIBRATION_BASE, CALIBRATION_BASE
assert "даже если их 10, 20 или больше" in CALIBRATION_BASE, CALIBRATION_BASE
print("[OK] the calibration text also explicitly covers the SAME problem occurring several times inside "
      "one long, multi-sentence pair (e.g. one Excel cell with a whole paragraph) — every occurrence must "
      "be its own separate finding, quoting which sentence/fragment it's in, never blended into one")

# --- Александр's ask, 2026-09-18: shorten the AI's own written findings —
# the model's output is the pricier side of the token bill (several times
# the input rate), so a terser "message" cuts cost without touching what
# counts as a real finding. _calibration (folded into both prompts via
# {calibration}) now carries an explicit brevity instruction with a worked
# before/after example matching Александр's own wording. ---
calib_any = _calibration(["typo"])
assert "МАКСИМАЛЬНО КОРОТКО" in calib_any, calib_any
assert "«Secure position» — «закрепите место»" in calib_any, calib_any
assert "Сокращай только форму, а не суть" in calib_any, calib_any
print("[OK] the calibration text now explicitly asks the model to keep \"message\" as short as possible "
      "while staying clear about which exact phrase and what difference it's about, with a worked "
      "before/after example — output tokens are the expensive side of the bill, so this is a pure "
      "writing-style instruction that doesn't loosen or change what counts as a real finding")

# --- the prompt also tells the model not to squeeze an out-of-scope
# finding into whichever type happens to be the only one allowed —
# Александр hit exactly this with the (now-removed) glossary check, but
# the instruction itself is generic, not glossary-specific, so it stays
# relevant for any single-check-type run. As of 2026-09-23 this text isn't
# hard-coded into the templates any more — it's the dynamic
# {other_type_instruction} placeholder (see OTHER_TYPE above), so it's
# absent whenever there's no real check list to be outside of (a
# register-only run) instead of always present. ---
from app.claude_client import SINGLE_PROMPT, BATCH_PROMPT, BATCH_PROMPT_SINGLE_ITEM, run_ai_checks

assert "{other_type_instruction}" in SINGLE_PROMPT and "{other_type_instruction}" in BATCH_PROMPT, (
    "both templates must carry the dynamic placeholder rather than a hard-coded copy of the instruction"
)
assert "{other_type_instruction}" in BATCH_PROMPT_SINGLE_ITEM

_squeeze_test_prompts = {"typo": [], "register": []}


async def _fake_call_claude_records_prompt_typo(prompt, model=None):
    _squeeze_test_prompts["typo"].append(prompt)
    return "[]", {"input_tokens": 10, "output_tokens": 2}, "end_turn"


async def _fake_call_claude_records_prompt_register(prompt, model=None):
    _squeeze_test_prompts["register"].append(prompt)
    return '[{"row": 1, "type": "register_value", "severity": "low", "value": "formal", "message": ""}]', {"input_tokens": 10, "output_tokens": 5}, "end_turn"


settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
claude_client_mod._call_claude = _fake_call_claude_records_prompt_typo
asyncio.get_event_loop().run_until_complete(
    run_ai_checks("source", "translation", ["typo"], target_lang="ru")
)
claude_client_mod._call_claude = _fake_call_claude_records_prompt_register
asyncio.get_event_loop().run_until_complete(
    run_ai_checks("source", "translation", ["register"], target_lang="ru")
)
claude_client_mod._call_claude = _previous_call_claude
settings.ANTHROPIC_API_KEY = ""

assert "не подгоняй" in _squeeze_test_prompts["typo"][0], (
    "with a real check selected, the formatted prompt must actually carry the out-of-scope instruction"
)
assert "не подгоняй" not in _squeeze_test_prompts["register"][0], (
    "a register-only run has no real \"Что проверять\" list to be outside of, so the out-of-scope "
    "instruction must be absent entirely, not just unused"
)
print("[OK] the prompt explicitly forbids squeezing an out-of-scope finding into whichever type happens to "
      "be the only one allowed, via the dynamic {other_type_instruction} placeholder rather than a "
      "hard-coded copy in every template — and it's correctly absent for a register-only run")

# --- BATCH_PROMPT explicitly tells the model how to report the SAME exact
# problem repeating identically across several pairs (Александр's ask,
# 2026-09-17: e.g. a tournament name "Fly & Win" mistranslated the same
# way in 5 rows shouldn't come back as 5 separate, near-duplicate
# findings) — via "rows": [...] instead of "row", with a message starting
# "Повторяется по всему документу: ...". group_batch_findings must turn
# that into ONE finding attached to the FIRST of those rows, carrying
# every other one's item index internally (as "_also_idx") for
# app.excel_multi to resolve into real Excel row numbers later — not lost,
# not duplicated across every row it lists. ---
assert "Повторяется по всему документу" in BATCH_PROMPT, BATCH_PROMPT
assert '"rows"' in BATCH_PROMPT, BATCH_PROMPT
# the repeat-aggregation rule explicitly reconciles itself with the prompt's own "evaluate each pair
# separately" opening line, and explicitly tells the model to fall back to an ordinary "row" finding if
# filtering a partial repeat down to where it's actually present leaves only one pair (review findings —
# both were "plausible" prompt-ambiguity risks, not confirmed model failures, but cheap to close off)
assert "НЕ противоречит" in BATCH_PROMPT, BATCH_PROMPT
assert "остаётся только ОДНА пара" in BATCH_PROMPT, BATCH_PROMPT
# and the cross-row "rows" repeat mechanism explicitly disclaims the OTHER
# kind of repeat (Александр's ask, 2026-09-17) — several occurrences of the
# same problem INSIDE one pair's own text — so the model doesn't try to
# jam the same pair number into "rows" several times, or merge them,
# instead of just reporting each occurrence as its own ordinary "row" finding.
assert "ВНУТРИ одной и той же пары" in BATCH_PROMPT, BATCH_PROMPT
assert "не объединяй их в одну находку" in BATCH_PROMPT, BATCH_PROMPT

# --- Александр's real feedback (2026-09-17): the model's own free-text
# "message" was citing its own internal, per-request pair numbers ("пара
# 2", "остальных парах (6, 10, 13, 15)") — numbers that mean nothing to
# the manager reading the report, since they're positions inside THIS
# prompt's own "Пары для проверки" list, not the real Excel row numbers
# the report actually shows (those are handled separately, via "row"/
# "rows" and the automatic "также в строках: …" suffix). The prompt must
# explicitly forbid this, not just implicitly rely on the model doing the
# sensible thing. ---
assert "НИКОГДА не упоминай в нём номер пары/строки" in BATCH_PROMPT, BATCH_PROMPT
assert "без номеров пар/строк" in BATCH_PROMPT, BATCH_PROMPT
print("[OK] BATCH_PROMPT explicitly forbids citing internal pair/row numbers inside a finding's own "
      "\"message\" text — those numbers are meaningless to the manager and don't match real Excel rows; "
      "real row attribution is handled separately by \"row\"/\"rows\" and the automatic "
      "\"также в строках\" suffix")

from app.claude_client import group_batch_findings

_gbf_map = {1: 10, 2: 11, 3: 12, 4: 13, 5: 14}  # 1-based prompt row -> item index
_gbf_raw = [
    {"row": 2, "type": "typo", "severity": "medium", "message": "обычная опечатка только в этой паре"},
    {
        "rows": [1, 3, 5],
        "type": "untranslatable",
        "severity": "medium",
        "message": "Повторяется по всему документу: «Fly & Win» переведено, должно остаться как есть",
    },
]
_gbf_grouped = group_batch_findings(_gbf_raw, _gbf_map)
# only TWO top-level keys: the "rows" entry's FIRST index (10) and the
# ordinary "row" entry's index (11) — items 12/14 (the other two rows the
# repeated problem also names) are NOT separate top-level entries, only
# referenced inside the first one's "_also_idx", so they never show up
# duplicated in the grouped dict's own keys.
assert set(_gbf_grouped.keys()) == {10, 11}, _gbf_grouped
assert _gbf_grouped[10][0]["type"] == "untranslatable" and "_also_idx" in _gbf_grouped[10][0], _gbf_grouped
assert _gbf_grouped[10][0]["_also_idx"] == [12, 14], _gbf_grouped  # items 3 and 5's own indices, in order
assert "rows" not in _gbf_grouped[10][0] and "row" not in _gbf_grouped[10][0], _gbf_grouped
assert _gbf_grouped[11][0]["message"] == "обычная опечатка только в этой паре", _gbf_grouped
# an unresolvable "rows" list (every number missing from the map) is dropped rather than guessed at
assert group_batch_findings([{"rows": [99], "type": "typo", "severity": "low", "message": "x"}], _gbf_map) == {}
# a single-element "rows" list is exactly equivalent to an ordinary "row" finding — no "_also_idx" at all
_gbf_single = group_batch_findings([{"rows": [2], "type": "typo", "severity": "low", "message": "x"}], _gbf_map)
assert set(_gbf_single.keys()) == {11} and "_also_idx" not in _gbf_single[11][0], _gbf_single
# a non-list "rows" value (model misformats it, e.g. as a bare number or string) doesn't match the
# isinstance(list) check, falls through to the ordinary "row" lookup, finds no "row" key either, and is
# dropped rather than crashing or being guessed at
assert group_batch_findings([{"rows": 2, "type": "typo", "severity": "low", "message": "x"}], _gbf_map) == {}
assert group_batch_findings([{"rows": "2", "type": "typo", "severity": "low", "message": "x"}], _gbf_map) == {}
# a "rows" list with a duplicated number (model repeats itself, e.g. [3, 3, 7]) still resolves — and,
# because the group key is always indices[0], the duplicate can end up INSIDE "_also_idx" alongside the
# entry's own index; this is exactly the shape that used to cause the self-reference bug below, so
# app.excel_multi._resolve_repeated_findings (not group_batch_findings itself) is what has to cope with it.
_gbf_dup = group_batch_findings(
    [{"rows": [2, 2, 4], "type": "untranslatable", "severity": "medium", "message": "Повторяется по всему документу: x"}],
    _gbf_map,
)
assert set(_gbf_dup.keys()) == {11}, _gbf_dup
assert _gbf_dup[11][0]["_also_idx"] == [11, 13], _gbf_dup  # item 2's own index (11) shows up here too
print("[OK] group_batch_findings: a \"rows\"-tagged entry (the same repeated problem across several "
      "pairs) is attached once, to its first row, carrying every other row's index internally for later "
      "resolution — never duplicated across each row it lists, a single-row \"rows\" list behaves exactly "
      "like an ordinary \"row\" finding, a non-list \"rows\" value is dropped rather than crashing, and an "
      "ordinary single-\"row\" finding is completely unaffected")

# --- several SEPARATE occurrences of the same problem type, all reported
# against the SAME pair (Александр's ask, 2026-09-17: one long cell with
# 30 sentences, the same broken element in several of them) must all
# survive as distinct findings on that one row, not collapse into one —
# group_batch_findings just appends, so this is really locking in that no
# later step accidentally deduplicates same-type findings on one row. ---
_multi_in_row_raw = [
    {"row": 2, "type": "untranslatable", "severity": "medium", "message": "предложение 3: «Grand Prix» переведено"},
    {"row": 2, "type": "untranslatable", "severity": "medium", "message": "предложение 11: «Grand Prix» переведено"},
    {"row": 2, "type": "untranslatable", "severity": "medium", "message": "предложение 24: «Grand Prix» переведено"},
]
_multi_in_row_grouped = group_batch_findings(_multi_in_row_raw, _gbf_map)
assert len(_multi_in_row_grouped[11]) == 3, _multi_in_row_grouped
assert {f["message"] for f in _multi_in_row_grouped[11]} == {
    "предложение 3: «Grand Prix» переведено",
    "предложение 11: «Grand Prix» переведено",
    "предложение 24: «Grand Prix» переведено",
}, _multi_in_row_grouped
print("[OK] group_batch_findings: several separate same-type findings reported against the SAME pair "
      "(e.g. the same broken element repeating in several sentences of one long cell) all survive intact "
      "as distinct findings, never collapsed into one")

# --- app.excel_multi._resolve_repeated_findings is what turns that
# internal "_also_idx" into the ACTUAL Excel row numbers the manager reads
# in the report (item indices mean nothing to them), folded into the
# finding's own message, with the internal key stripped so it never
# leaks into a response. ---
from app.excel_multi import _resolve_repeated_findings

_rrf_rows = [{"excel_row": 5}, {"excel_row": 6}, {"excel_row": 9}, {"excel_row": 12}, {"excel_row": 20}]
_rrf_resolved = _resolve_repeated_findings(
    {0: [{"type": "untranslatable", "severity": "medium", "message": "Повторяется по всему документу: ...",
          "_also_idx": [2, 4]}]},
    _rrf_rows,
)
assert "_also_idx" not in _rrf_resolved[0][0], _rrf_resolved
assert _rrf_resolved[0][0]["message"] == "Повторяется по всему документу: ... (также в строках: 9, 20)", _rrf_resolved
# a finding with no "_also_idx" at all (the ordinary case) passes through unchanged
assert _resolve_repeated_findings({0: [{"type": "typo", "message": "m"}]}, _rrf_rows) == {0: [{"type": "typo", "message": "m"}]}
# self-reference bug (found by review, since fixed): if "_also_idx" names the SAME item index the
# finding is already keyed under (e.g. group_batch_findings resolved a duplicated "rows" number back to
# its own index — see the group_batch_findings test above), the finding's own Excel row must NOT show up
# in its own "также в строках: ..." list — a row can't "also" repeat in itself.
_rrf_self = _resolve_repeated_findings(
    {0: [{"type": "untranslatable", "severity": "medium", "message": "Повторяется по всему документу: ...",
          "_also_idx": [0, 2]}]},  # 0 is this finding's OWN index, alongside the real other occurrence (2)
    _rrf_rows,
)
assert _rrf_self[0][0]["message"] == "Повторяется по всему документу: ... (также в строках: 9)", _rrf_self
# and if EVERY index in "_also_idx" turns out to be self-referencing, there's nothing left to name at
# all — the message is left exactly as the model wrote it, no dangling "(также в строках: )"
_rrf_all_self = _resolve_repeated_findings(
    {0: [{"type": "untranslatable", "severity": "medium", "message": "Повторяется по всему документу: ...",
          "_also_idx": [0]}]},
    _rrf_rows,
)
assert _rrf_all_self[0][0]["message"] == "Повторяется по всему документу: ...", _rrf_all_self
print("[OK] _resolve_repeated_findings: the internal row-index list is turned into the actual Excel row "
      "numbers and folded into the finding's own message, with the internal key never leaking through — "
      "an ordinary finding with nothing to resolve passes through untouched, and a self-referencing index "
      "(the finding's own row named inside its own \"_also_idx\") is correctly excluded rather than making "
      "a row's message claim it also repeats in itself")

# --- end-to-end through the live multi-check path: a mocked batch
# response using "rows" for one repeated problem (rows 1 and 3) plus an
# ordinary "row" finding (row 2) — the repeated one must be attached to
# its FIRST occurrence (excel_row 2), naming the other one (excel_row 5)
# in its own message, and excel_row 5 must NOT show that same finding
# a second time. ---
from app.excel_multi import _check_language_for_sheet


async def _fake_call_claude_repeated_batch(prompt, model=None):
    return (
        '[{"row": 2, "type": "typo", "severity": "low", "message": "мелкая опечатка только здесь"},'
        '{"rows": [1, 3], "type": "untranslatable", "severity": "medium", '
        '"message": "Повторяется по всему документу: «Fly & Win» переведено"}]',
        {"input_tokens": 30, "output_tokens": 30},
        "end_turn",
    )


_rep_sheet = {
    "sheet_name": "Sheet1",
    "languages": ["en", "ru"],
    "rows": [
        {"excel_row": 2, "context": "title 1", "max_length": None, "values": {"en": "Fly & Win", "ru": "Лети и выигрывай"}},
        {"excel_row": 3, "context": "body", "max_length": None, "values": {"en": "Terms apply", "ru": "Условия дейстуют"}},
        {"excel_row": 5, "context": "title 2", "max_length": None, "values": {"en": "Fly & Win", "ru": "Лети и выигрывай"}},
    ],
}
settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
claude_client_mod._call_claude = _fake_call_claude_repeated_batch
_rep_out, _rep_cost = asyncio.get_event_loop().run_until_complete(
    _check_language_for_sheet(_rep_sheet, "ru", "en", ["typo", "untranslatable"], "", asyncio.Semaphore(5))
)
claude_client_mod._call_claude = _previous_call_claude
settings.ANTHROPIC_API_KEY = ""
# excel_row 5 has NOTHING of its own to show — the repeated finding lives
# only at its first occurrence (excel_row 2), so only rows 2 and 3 show up
# at all; row 5 isn't in the output, which IS the point of not repeating
# the same finding a second time.
assert {r["excel_row"] for r in _rep_out} == {2, 3}, _rep_out
_rep_by_row = {r["excel_row"]: r["findings"] for r in _rep_out}
_rep_repeated = [f for f in _rep_by_row[2] if f["type"] == "untranslatable"]
assert len(_rep_repeated) == 1, _rep_by_row[2]
assert _rep_repeated[0]["message"] == "Повторяется по всему документу: «Fly & Win» переведено (также в строках: 5)", _rep_by_row[2]
print("[OK] multi-check live path: a \"rows\"-tagged repeated finding shows up once, at its first "
      "occurrence, naming the other Excel row(s) it also applies to in its own message — the other row(s) "
      "don't show the same finding a second time")

# --- same thing, but through the OTHER path: finalize_batch_results, used for large uploads that go "
# through the Anthropic Message Batches API instead of a synchronous call. Only the live path above was "
# --- exercised end-to-end before (review finding — coverage gap). Skeleton shape matches what "
# build_batch_plan actually produces (see excel_multi.build_batch_plan). ---
from app.excel_multi import finalize_batch_results

_fbr_skeleton = {
    "sheets": [{
        "sheet_name": "Sheet1",
        "target_langs": ["ru"],
        "languages": {
            "ru": {
                "model": "claude-sonnet-4-5-20250929",
                "chunks": [{"custom_id": "s0-t0-c0", "number_to_index": {"1": 0, "2": 1, "3": 2}, "row_offset": 0}],
                "rows": [
                    {"excel_row": 2, "context": "title 1", "source": "Fly & Win", "translation": "Лети и выигрывай", "findings": []},
                    {"excel_row": 3, "context": "body", "source": "Terms apply", "translation": "Условия дейстуют", "findings": []},
                    {"excel_row": 5, "context": "title 2", "source": "Fly & Win", "translation": "Лети и выигрывай", "findings": []},
                ],
            },
        },
        "unrecognized_columns": [],
        "row_count": 3,
    }],
    "source_lang": "en",
    "checks": ["typo", "untranslatable"],
}
_fbr_results = {
    "s0-t0-c0": {
        "text": (
            '[{"row": 2, "type": "typo", "severity": "low", "message": "мелкая опечатка только здесь"},'
            '{"rows": [1, 3], "type": "untranslatable", "severity": "medium", '
            '"message": "Повторяется по всему документу: «Fly & Win» переведено"}]'
        ),
        "usage": {"input_tokens": 30, "output_tokens": 30},
        "result_type": "succeeded",
        "stop_reason": "end_turn",
    },
}
_fbr_out = finalize_batch_results(_fbr_skeleton, _fbr_results)
_fbr_lang = _fbr_out["sheets"][0]["languages"]["ru"]
# same expectation as the live-path test: excel_row 5 has nothing of its own to show, only 2 and 3 appear
assert {r["excel_row"] for r in _fbr_lang} == {2, 3}, _fbr_lang
_fbr_by_row = {r["excel_row"]: r["findings"] for r in _fbr_lang}
_fbr_repeated = [f for f in _fbr_by_row[2] if f["type"] == "untranslatable"]
assert len(_fbr_repeated) == 1, _fbr_by_row[2]
assert _fbr_repeated[0]["message"] == "Повторяется по всему документу: «Fly & Win» переведено (также в строках: 5)", _fbr_by_row[2]
print("[OK] finalize_batch_results (Message Batches / large-file path): a \"rows\"-tagged repeated "
      "finding is resolved exactly the same way as on the live synchronous path — once, at its first "
      "occurrence, naming the other Excel row(s) in its own message")

# --- MAX_ROWS_PER_AI_CALL / _chunk_list: splitting one language's rows into
# smaller AI calls instead of one giant one (Александр's ask, 2026-09-17: a
# document check on a large language missed a subtle, meaning-based
# problem that pasting the very same text into the single-pair fields DID
# catch — most likely cause, a single AI call covering hundreds of rows at
# once has to split its attention across all of them). ---
from app.excel_multi import MAX_ROWS_PER_AI_CALL, _chunk_list

assert _chunk_list([], 5) == []
assert _chunk_list([1, 2, 3], 5) == [[1, 2, 3]]
assert _chunk_list(list(range(7)), 3) == [[0, 1, 2], [3, 4, 5], [6]]
print(f"[OK] _chunk_list: splits into consecutive chunks of at most the given size, an empty list "
      f"yields no chunks at all (MAX_ROWS_PER_AI_CALL is currently {MAX_ROWS_PER_AI_CALL})")

# Live path: a language with MORE rows than MAX_ROWS_PER_AI_CALL must result
# in more than one AI call (one per chunk), and every chunk's findings must
# land on the CORRECT excel_row once its local, per-chunk item indices are
# shifted back by that chunk's own offset — including a "rows"-tagged
# repeat WITHIN one chunk (still merged there, offset correctly), while a
# look-alike problem sitting in a DIFFERENT chunk is deliberately NOT
# merged with it, because that chunk's own AI call never saw the first
# chunk's rows at all (the trade-off documented on MAX_ROWS_PER_AI_CALL).
_chunk_call_prompts: list[str] = []


async def _fake_call_claude_chunked(prompt, model=None):
    _chunk_call_prompts.append(prompt)
    if len(_chunk_call_prompts) == 1:
        # first chunk: local rows 1 and 3 share the same repeated problem
        return (
            '[{"rows": [1, 3], "type": "untranslatable", "severity": "medium", '
            '"message": "Повторяется по всему документу: «Fly & Win» переведено"}]',
            {"input_tokens": 20, "output_tokens": 20},
            "end_turn",
        )
    # second chunk: the exact same wording, but on an isolated single row —
    # must NOT get merged with the first chunk's repeat
    return (
        '[{"row": 1, "type": "untranslatable", "severity": "medium", '
        '"message": "Повторяется по всему документу: «Fly & Win» переведено"}]',
        {"input_tokens": 20, "output_tokens": 20},
        "end_turn",
    )


_chunk_rows = [
    {"excel_row": 100 + i, "context": f"row {i}", "max_length": None,
     "values": {"en": "Fly & Win", "ru": "Лети и выигрывай"}}
    for i in range(MAX_ROWS_PER_AI_CALL + 3)  # spills into a second, smaller chunk
]
_chunk_sheet = {"sheet_name": "Sheet1", "languages": ["en", "ru"], "rows": _chunk_rows}
settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
claude_client_mod._call_claude = _fake_call_claude_chunked
_chunk_out, _chunk_cost = asyncio.get_event_loop().run_until_complete(
    _check_language_for_sheet(_chunk_sheet, "ru", "en", ["untranslatable"], "", asyncio.Semaphore(5))
)
claude_client_mod._call_claude = _previous_call_claude
settings.ANTHROPIC_API_KEY = ""
assert len(_chunk_call_prompts) == 2, "expected exactly 2 AI calls — one per chunk"
_chunk_by_row = {r["excel_row"]: r["findings"] for r in _chunk_out}
# first chunk's repeat: local rows 1 and 3 -> global indices 0 and 2 -> excel_row 100 and 102
assert _chunk_by_row[100][0]["message"] == (
    "Повторяется по всему документу: «Fly & Win» переведено (также в строках: 102)"
), _chunk_by_row.get(100)
assert 102 not in _chunk_by_row, "the repeat's OTHER occurrence must not show its own separate finding"
# second chunk's finding: local row 1 of chunk 2 -> its own global index -> its own excel_row
_second_chunk_excel_row = _chunk_rows[MAX_ROWS_PER_AI_CALL]["excel_row"]
assert _chunk_by_row[_second_chunk_excel_row][0]["message"] == (
    "Повторяется по всему документу: «Fly & Win» переведено"
), _chunk_by_row.get(_second_chunk_excel_row)
print(f"[OK] live multi-check path: a language with more rows than MAX_ROWS_PER_AI_CALL "
      f"({MAX_ROWS_PER_AI_CALL}) is split into several smaller AI calls instead of one giant one, every "
      f"chunk's findings land on the correct excel_row once local indices are shifted back by that chunk's "
      f"own offset, and a repeat within one chunk is still merged there — but a look-alike problem in a "
      f"DIFFERENT chunk is correctly left unmerged, since that chunk's AI call never saw the first chunk's "
      f"rows")

# MAX_ROWS_PER_AI_CALL_HARD: a real Marathi miss (2026-09-22) — the exact
# same pair, same model (Opus, since mr is on HARD_LANGUAGE_BASES), same
# prompt — was caught when checked ALONE via the single-pair form but
# MISSED as part of a normal batched multi-check. Confirms MAX_ROWS_PER_AI_CALL
# (15) is still too many rows at once for the hardest languages specifically,
# so those now get their own, much smaller chunk size (1 row — only ever
# actually proven, not a guessed middle value) while every other language
# keeps the normal size.
from app.excel_multi import MAX_ROWS_PER_AI_CALL_HARD, _chunk_size_for_lang

assert MAX_ROWS_PER_AI_CALL_HARD == 1
assert _chunk_size_for_lang("mr") == MAX_ROWS_PER_AI_CALL_HARD, "mr (Marathi) is on HARD_LANGUAGE_BASES"
assert _chunk_size_for_lang("mr-IN") == MAX_ROWS_PER_AI_CALL_HARD, "region variants of a hard base must match too"
assert _chunk_size_for_lang("ky") == MAX_ROWS_PER_AI_CALL_HARD, "ky (Kyrgyz) is on HARD_LANGUAGE_BASES"
assert _chunk_size_for_lang("ru") == MAX_ROWS_PER_AI_CALL, "ru is NOT a hard language — normal chunk size"
assert _chunk_size_for_lang("es-mx") == MAX_ROWS_PER_AI_CALL, "an easy language's region variant is unaffected"

_hard_chunk_rows = [
    {"excel_row": 400 + i, "context": f"row {i}", "max_length": None,
     "values": {"ru": "Фрибет без отыгрыша", "mr": f"पैज न लावता {i}"}}
    for i in range(3)
]
_hard_chunk_sheet = {"sheet_name": "Sheet1", "languages": ["ru", "mr"], "rows": _hard_chunk_rows}
_hard_chunk_calls = {"n": 0}


async def _fake_call_claude_count_calls(prompt, model=None):
    _hard_chunk_calls["n"] += 1
    return "[]", {"input_tokens": 20, "output_tokens": 5}, "end_turn"


settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
claude_client_mod._call_claude = _fake_call_claude_count_calls
asyncio.get_event_loop().run_until_complete(
    _check_language_for_sheet(_hard_chunk_sheet, "mr", "ru", ["typo"], "", asyncio.Semaphore(5))
)
claude_client_mod._call_claude = _previous_call_claude
settings.ANTHROPIC_API_KEY = ""
assert _hard_chunk_calls["n"] == 3, (
    f"a hard language (mr) with 3 rows must make 3 SEPARATE AI calls (1 row each, MAX_ROWS_PER_AI_CALL_HARD), "
    f"not 1 batched call — got {_hard_chunk_calls['n']} calls"
)
print("[OK] MAX_ROWS_PER_AI_CALL_HARD: a hard-list language (mr, ky, ...) is checked ONE row at a time — proven "
      "necessary by a real Marathi miss that a single-pair check caught but a same-model, same-prompt batched "
      "check didn't — while every other language keeps the normal, larger MAX_ROWS_PER_AI_CALL chunk size")

# BATCH_PROMPT_SINGLE_ITEM: Александр's SHARPER follow-up test, 2026-09-22 —
# even a 1-ROW document upload (so MAX_ROWS_PER_AI_CALL_HARD's chunking
# fix above doesn't even come into play; it was already exactly 1 row)
# still missed the same Marathi finding that the single-pair FIELDS form
# caught reliably. Proved it's not about row COUNT at all — build_batch_prompt
# always used the full BATCH_PROMPT text, which spends a large block on
# cross-row duplicate detection that's meaningless with only one pair to
# compare against nothing. Now build_batch_prompt uses a leaner template
# (BATCH_PROMPT_SINGLE_ITEM) whenever there's exactly one checkable item —
# same "row"-numbered JSON shape (so group_batch_findings needs no
# special-casing), just without the irrelevant cross-row instructions.
from app.claude_client import build_batch_prompt as _build_batch_prompt_direct
from app.claude_client import BATCH_PROMPT_SINGLE_ITEM

_one_item = [{"context": "freebet", "source": "Фрибет без отыгрыша", "translation": "पैज न लावता फ्री बेट"}]
_one_prompt, _one_map = _build_batch_prompt_direct(_one_item, ["typo"], "", "mr", "ru")
assert "Повторяется по всему документу" not in _one_prompt, (
    "a single-item prompt must NOT include the cross-row duplicate-detection instructions — there's nothing "
    "to compare against with only one pair, and Александр's real test showed this extra text costs accuracy"
)
assert "Дана одна пара" in _one_prompt, "a single-item prompt must use BATCH_PROMPT_SINGLE_ITEM, not BATCH_PROMPT"
assert '"row": 1' in _one_prompt, "the single-item prompt must still ask for the SAME row-numbered JSON shape"
assert _one_map == {1: 0}, "number_to_index must still map correctly for the single-item case"

_two_items = [
    {"context": "a", "source": "Hello", "translation": "Привет"},
    {"context": "b", "source": "World", "translation": "Мир"},
]
_two_prompt, _two_map = _build_batch_prompt_direct(_two_items, ["typo"], "", "ru", "en")
assert "Повторяется по всему документу" in _two_prompt, (
    "a real multi-item batch must still get the full cross-row duplicate-detection instructions — this is "
    "ONLY skipped for the single-item case, not lost for genuine batches"
)
assert "Дана одна пара" not in _two_prompt
assert _two_map == {1: 0, 2: 1}

# Two review-caught defects, both fixed before shipping: (1) the single-item
# template must still forbid citing internal row/pair numbers inside a
# finding's own "message" text — dropped by accident when BATCH_PROMPT_SINGLE_ITEM
# was first written; (2) register instructions (batch=True) referenced "Пары
# для проверки" — a section that doesn't exist at all in the single-item
# template, which uses plain Контекст/Исходный текст/Перевод fields instead.
assert "НИКОГДА не упоминай" in _one_prompt, (
    "the single-item prompt must still forbid citing internal row/pair numbers in a finding's message text"
)
_one_prompt_reg, _ = _build_batch_prompt_direct(_one_item, ["typo", "register"], "", "mr", "ru")
assert "Пары для проверки" not in _one_prompt_reg, (
    f"register instructions for a single-item prompt must NOT reference the (nonexistent, in this template) "
    f"«Пары для проверки» list — got {_one_prompt_reg!r}"
)
assert '"row": 1' in _one_prompt_reg and "register_value" in _one_prompt_reg, (
    "the single-item register instructions must still ask for a row-numbered register_value entry, matching "
    "the JSON shape group_batch_findings/_extract_register_values expect"
)
_two_prompt_reg, _ = _build_batch_prompt_direct(_two_items, ["typo", "register"], "", "ru", "en")
assert "Пары для проверки" in _two_prompt_reg, (
    "a real multi-item batch's register instructions must still reference the pairs list as before"
)
print("[OK] BATCH_PROMPT_SINGLE_ITEM: a document check with exactly ONE checkable row now gets a leaner "
      "prompt (no cross-row duplicate-detection instructions, which are meaningless with nothing to compare "
      "against) instead of the full BATCH_PROMPT text — a real multi-row batch is completely unaffected and "
      "still gets the full instructions, and the JSON response shape (\"row\"-numbered) stays identical "
      "either way so downstream parsing needs no special-casing")

# Message Batches (large-file) path: the same chunking, but through
# build_batch_plan/finalize_batch_results — one custom_id per chunk, and a
# chunk that never came back (missing) must not swallow another chunk's
# perfectly good findings, while still surfacing exactly one warning.
from app.excel_multi import build_batch_plan

_bpc_rows = [
    {"excel_row": 200 + i, "context": f"row {i}", "max_length": None,
     "values": {"en": "Fly & Win", "ru": "Лети и выигрывай"}}
    for i in range(MAX_ROWS_PER_AI_CALL + 2)
]
_bpc_sheet = {"sheet_name": "Sheet1", "languages": ["en", "ru"], "rows": _bpc_rows}
_bpc_requests, _bpc_skeleton = build_batch_plan([_bpc_sheet], "en", ["untranslatable"], "", None)
assert len(_bpc_requests) == 2, "expected one request per chunk — 17 rows over a 15-row cap is 2 chunks"
_bpc_chunks = _bpc_skeleton["sheets"][0]["languages"]["ru"]["chunks"]
assert len(_bpc_chunks) == 2, _bpc_chunks
_bpc_id0, _bpc_id1 = _bpc_chunks[0]["custom_id"], _bpc_chunks[1]["custom_id"]
_bpc_results = {
    _bpc_id0: {
        "text": '[{"row": 1, "type": "untranslatable", "severity": "medium", "message": "проблема в первом чанке"}]',
        "usage": {"input_tokens": 20, "output_tokens": 20}, "stop_reason": "end_turn", "result_type": "succeeded",
    },
    # _bpc_id1 deliberately absent — dropped between submit and poll
}
_bpc_out = finalize_batch_results(_bpc_skeleton, _bpc_results)
_bpc_lang = _bpc_out["sheets"][0]["languages"]["ru"]
_bpc_by_row = {r["excel_row"]: r["findings"] for r in _bpc_lang}
# the first chunk's real finding must still show up, even though the second chunk's result never arrived
assert any(f["message"] == "проблема в первом чанке" for f in _bpc_by_row.get(200, [])), _bpc_by_row
# exactly one system warning for the whole language, not one per chunk
_bpc_warnings = [f for row in _bpc_lang for f in row["findings"] if f.get("type") == "system"]
assert len(_bpc_warnings) == 1, _bpc_lang
print("[OK] finalize_batch_results: a language split into several chunks merges every chunk's own custom_id "
      "independently — one missing/errored/truncated chunk doesn't discard another chunk's good findings, "
      "and only a single system warning is shown for the whole language rather than one per chunk")

# Same idea, but proving the "_also_idx" (repeated-finding) offset remap is
# correct on the BATCH path specifically, not just the live path above —
# the review pass that caught the earlier self-reference bug flagged this
# exact combination (chunking + "rows") as untested on this path.
_bpr_rows = [
    {"excel_row": 300 + i, "context": f"row {i}", "max_length": None,
     "values": {"en": "Fly & Win", "ru": "Лети и выигрывай"}}
    for i in range(MAX_ROWS_PER_AI_CALL + 2)
]
_bpr_sheet = {"sheet_name": "Sheet1", "languages": ["en", "ru"], "rows": _bpr_rows}
_bpr_requests, _bpr_skeleton = build_batch_plan([_bpr_sheet], "en", ["untranslatable"], "", None)
_bpr_chunks = _bpr_skeleton["sheets"][0]["languages"]["ru"]["chunks"]
assert len(_bpr_chunks) == 2, _bpr_chunks
_bpr_results = {
    # first chunk: local rows 1 and 3 repeat the same problem
    _bpr_chunks[0]["custom_id"]: {
        "text": (
            '[{"rows": [1, 3], "type": "untranslatable", "severity": "medium", '
            '"message": "Повторяется по всему документу: «Fly & Win» переведено"}]'
        ),
        "usage": {"input_tokens": 20, "output_tokens": 20}, "stop_reason": "end_turn", "result_type": "succeeded",
    },
    _bpr_chunks[1]["custom_id"]: {
        "text": "[]", "usage": {"input_tokens": 10, "output_tokens": 5}, "stop_reason": "end_turn", "result_type": "succeeded",
    },
}
_bpr_out = finalize_batch_results(_bpr_skeleton, _bpr_results)
_bpr_by_row = {r["excel_row"]: r["findings"] for r in _bpr_out["sheets"][0]["languages"]["ru"]}
# local rows 1 and 3 of chunk 0 -> global indices 0 and 2 -> excel_row 300 and 302
assert _bpr_by_row[300][0]["message"] == (
    "Повторяется по всему документу: «Fly & Win» переведено (также в строках: 302)"
), _bpr_by_row.get(300)
assert 302 not in _bpr_by_row, "the repeat's OTHER occurrence must not show its own separate finding"
print("[OK] finalize_batch_results: a \"rows\"-tagged repeat's internal \"_also_idx\" is correctly shifted "
      "by the chunk's own row_offset on the Message Batches path too, not just the live path")

# Backward compatibility: a Message Batch submitted BEFORE this chunking
# change existed was persisted with the OLD skeleton shape (a bare
# "custom_id"/"number_to_index" pair directly on the language, no "chunks"
# list at all — see build_batch_plan's history). Any such upload still
# "processing" across a deploy of this change must still resolve correctly
# once its batch ends, rather than silently coming back "clean" because the
# new code went looking for a "chunks" key the old skeleton never had.
_oldshape_skeleton = {
    "sheets": [{
        "sheet_name": "Sheet1",
        "target_langs": ["ru"],
        "languages": {
            "ru": {
                "custom_id": "s0-t0",  # OLD shape: no "chunks" list
                "model": "claude-sonnet-4-5-20250929",
                "number_to_index": {"1": 0},
                "rows": [{"excel_row": 9, "context": "", "source": "Fly & Win", "translation": "Лети", "findings": []}],
            },
        },
        "unrecognized_columns": [],
        "row_count": 1,
    }],
    "source_lang": "en",
    "checks": ["untranslatable"],
}
_oldshape_results = {
    "s0-t0": {
        "text": '[{"row": 1, "type": "untranslatable", "severity": "medium", "message": "старый формат ещё работает"}]',
        "usage": {"input_tokens": 15, "output_tokens": 15}, "stop_reason": "end_turn", "result_type": "succeeded",
    },
}
_oldshape_out = finalize_batch_results(_oldshape_skeleton, _oldshape_results)
_oldshape_lang = _oldshape_out["sheets"][0]["languages"]["ru"]
assert any(
    f["message"] == "старый формат ещё работает" for row in _oldshape_lang for f in row["findings"]
), _oldshape_lang
print("[OK] finalize_batch_results: a batch submitted before chunking existed (old skeleton shape, no "
      "\"chunks\" key) still resolves its real AI findings correctly instead of silently coming back clean")

# --- AI findings are hard-filtered to only the checks actually requested,
# even if the model ignores the prompt's instruction and reports something
# else anyway ---
from app.claude_client import _allowed_ai_types, _filter_findings_by_checks, run_ai_checks
import app.claude_client as claude_client_mod

assert _allowed_ai_types(["typo"]) == {"typo", "other"}
assert _allowed_ai_types(["typo", "punctuation", "max_length"]) == {"typo", "other"}  # rule checks aren't AI types

raw_findings = [
    {"type": "typo", "severity": "medium", "message": "неверная валюта в переводе"},
    {"type": "untranslatable", "severity": "high", "message": "слово не переведено"},
]
assert _filter_findings_by_checks(raw_findings, ["typo"]) == [raw_findings[0]]

settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
captured_prompts = []


async def _fake_call_claude(prompt, model=None):
    captured_prompts.append(prompt)
    # Simulates a model that ignores "проверяй только typo" and
    # reports an untranslatable-text issue anyway.
    text = (
        '[{"type": "typo", "severity": "medium", "message": "неверная валюта в переводе"},'
        '{"type": "untranslatable", "severity": "high", "message": "слово не переведено"}]'
    )
    return (text, {"input_tokens": 500, "output_tokens": 100}, "end_turn")


claude_client_mod._call_claude = _fake_call_claude
findings, ai_cost = asyncio.get_event_loop().run_until_complete(
    run_ai_checks(
        "source", "translation", ["typo"], target_lang="az-az",
    )
)
assert findings == [raw_findings[0]], findings
assert ai_cost > 0, ai_cost
# The JSON schema shown to the model is also scoped down to just the
# requested check(s), not a fixed always-all list.
type_enum_line = next(
    line for line in captured_prompts[0].splitlines() if '"type":' in line and '"severity":' in line
)
assert "typo" in type_enum_line and "untranslatable" not in type_enum_line, type_enum_line
print("[OK] AI findings hard-filtered to requested checks even when the model reports "
      "an out-of-scope finding anyway (prompt's type list is also scoped down, in addition)")

# --- "other" (OTHER_TYPE) — Александр's ask, 2026-09-23: a genuinely
# serious out-of-scope finding must not be silently dropped, nor forced
# under the nearest wrong check type — it should land as its own,
# separately-tagged "other" finding instead. See app.claude_client's own
# comment above OTHER_TYPE. ---
from app.claude_client import OTHER_TYPE, _other_type_instruction, _checks_description

assert _allowed_ai_types(["register"]) == {"register_value"}, (
    "a register-only run has no real \"Что проверять\" list to be outside of, so OTHER_TYPE must NOT be added"
)
assert _other_type_instruction(_checks_description(["typo"])) != "", (
    "with a real check selected, the other-type instruction must actually be included"
)
assert _other_type_instruction(_checks_description(["register"])) == "", (
    "with only \"register\" selected (no real checks/description), the other-type instruction must be empty"
)

_other_raw_findings = [
    {"type": "typo", "severity": "medium", "message": "обычная опечатка"},
    {"type": OTHER_TYPE, "severity": "high", "message": "серьёзная проблема вне списка проверок"},
    {"type": "untranslatable", "severity": "high", "message": "не входит в выбранные проверки"},
]
assert _filter_findings_by_checks(_other_raw_findings, ["typo"]) == _other_raw_findings[:2], (
    "an \"other\" finding must survive the hard filter alongside a real requested-check finding, while a "
    "finding of a type that was never requested at all (untranslatable) is still dropped"
)


async def _fake_call_claude_other_type(prompt, model=None):
    return (
        '[{"type": "typo", "severity": "medium", "message": "обычная опечатка"},'
        '{"type": "other", "severity": "high", "message": "явная ошибка смысла вне списка проверок"}]',
        {"input_tokens": 40, "output_tokens": 20}, "end_turn",
    )


claude_client_mod._call_claude = _fake_call_claude_other_type
_other_findings, _ = asyncio.get_event_loop().run_until_complete(
    run_ai_checks("source", "translation", ["typo"], target_lang="az-az")
)
claude_client_mod._call_claude = _fake_call_claude
assert any(f["type"] == "other" for f in _other_findings), (
    f"an \"other\"-typed finding from the model must reach run_ai_checks's own output, not be filtered out — "
    f"got {_other_findings}"
)
assert len(_other_findings) == 2, (
    f"both the normal typo finding and the other-typed one must come through — got {_other_findings}"
)
print("[OK] \"other\": a genuinely out-of-scope finding survives the hard checks-filter and reaches the "
      "report tagged type=\"other\" instead of being silently dropped or forced under a wrong check type — "
      "and the instruction that makes this possible is only sent to the model when there's a real "
      "\"Что проверять\" list to be outside of in the first place (never for a register-only run)")

# --- a truncated JSON response (the model hit the max_tokens ceiling
# mid-array) must not lose every finding that came before the cut — only
# the incomplete trailing entry is dropped, everything complete survives —
# and callers must be told the response was truncated at all, rather than
# a partial result silently looking like a complete "checked, nothing
# else" report. Александр hit exactly this: a large Spanish multi-check
# came back with an incomplete report and no indication anything was cut
# short. ---
from app.claude_client import parse_json_array, _truncation_warning, _ai_failure_warning

truncated_text = (
    '[{"type": "typo", "severity": "medium", "message": "первая находка"},'
    '{"type": "typo", "severity": "high", "message": "вторая находка"},'
    '{"type": "typo", "sever'  # cut off mid-object, exactly as a max_tokens cutoff would do
)
salvaged = parse_json_array(truncated_text)
assert len(salvaged) == 2, salvaged
assert salvaged[0]["message"] == "первая находка" and salvaged[1]["message"] == "вторая находка", salvaged
print("[OK] parse_json_array: a JSON array cut off mid-object (max_tokens truncation) "
      "keeps every complete finding before the cut instead of losing the whole response")


async def _fake_call_claude_truncated(prompt, model=None):
    return ('[{"type": "typo", "severity": "medium", "message": "неверная валюта"}]',
            {"input_tokens": 500, "output_tokens": 100}, "max_tokens")


claude_client_mod._call_claude = _fake_call_claude_truncated
findings_trunc, _ = asyncio.get_event_loop().run_until_complete(
    run_ai_checks("source", "translation", ["typo"], target_lang="az-az")
)
assert any(f["type"] == "system" for f in findings_trunc), findings_trunc
claude_client_mod._call_claude = _fake_call_claude
print("[OK] run_ai_checks adds a visible system warning whenever the AI response was "
      "cut off by the max_tokens ceiling, instead of silently showing a partial result")

# --- same guarantee on the async Message Batches merge path
# (finalize_batch_results): a language whose batch response was truncated,
# or whose request errored/expired/never came back at all, gets a visible
# warning finding rather than silently showing as "checked, nothing found".
from app.excel_multi import finalize_batch_results

fake_skeleton = {
    "checks": ["typo"],
    "source_lang": "en",
    "sheets": [{
        "sheet_name": "Sheet1",
        "target_langs": ["es-mx", "fr", "de"],
        "unrecognized_columns": [],
        "row_count": 1,
        "languages": {
            "es-mx": {
                "model": "claude-haiku-4-5-20251001",
                "chunks": [{"custom_id": "s0-es-mx-c0", "number_to_index": {}, "row_offset": 0}],
                "rows": [{"excel_row": 2, "context": "", "source": "a", "translation": "b", "findings": []}],
            },
            "fr": {
                "model": "claude-haiku-4-5-20251001",
                "chunks": [{"custom_id": "s0-fr-c0", "number_to_index": {}, "row_offset": 0}],
                "rows": [{"excel_row": 2, "context": "", "source": "a", "translation": "b", "findings": []}],
            },
            "de": {
                # never made it into the results at all (e.g. dropped between submit and poll)
                "model": "claude-haiku-4-5-20251001",
                "chunks": [{"custom_id": "s0-de-c0", "number_to_index": {}, "row_offset": 0}],
                "rows": [{"excel_row": 2, "context": "", "source": "a", "translation": "b", "findings": []}],
            },
        },
    }],
}
fake_batch_results = {
    "s0-es-mx-c0": {"text": "[", "usage": {"input_tokens": 10, "output_tokens": 8000}, "stop_reason": "max_tokens", "result_type": "succeeded"},
    "s0-fr-c0": {"text": None, "usage": {}, "stop_reason": None, "result_type": "errored"},
    # "s0-de-c0" deliberately absent
}
merged = finalize_batch_results(fake_skeleton, fake_batch_results)
by_lang = merged["sheets"][0]["languages"]
assert any(f["type"] == "system" for row in by_lang["es-mx"] for f in row["findings"]), by_lang["es-mx"]
assert any(f["type"] == "system" for row in by_lang["fr"] for f in row["findings"]), by_lang["fr"]
assert any(f["type"] == "system" for row in by_lang["de"] for f in row["findings"]), by_lang["de"]
print("[OK] finalize_batch_results: a truncated, errored, or missing-entirely batch result "
      "each surface a visible system warning instead of silently reporting as clean")

# --- the /check endpoint itself surfaces the real AI cost, not just the
# internal run_ai_checks helper (Александр asked to see the cost of each
# check after it runs) ---
settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
r = check("standalone /check surfaces cost_usd for an AI-backed check", client.post(
    "/check",
    json={
        "source": "source text",
        "translation": "translation text",
        # "typo" needs no project document to run.
        "checks": ["typo"],
    },
))
check_cost_data = r.json()
assert check_cost_data["cost_usd"] > 0, check_cost_data
print(f"   /check cost_usd: {check_cost_data['cost_usd']}")
settings.ANTHROPIC_API_KEY = ""

# --- parse_workbook: obviously-not-a-language columns (per-channel
# character-limit spec columns, "ТЗ", etc.) are dropped silently instead of
# being dumped into the noisy "not recognized as languages" notice —
# Александр flagged a real file whose limit columns ("NOTIF title: 20",
# "Лимиты: PUSH banner: 25", ...) were cluttering that message even though
# it's obvious on sight they're not languages.
from app.excel_multi import parse_workbook
import openpyxl as _openpyxl

wb_limits = _openpyxl.Workbook()
ws_limits = wb_limits.active
ws_limits.title = "Sheet1"
ws_limits.append([
    "Context", "ТЗ", "Лимиты: NOTIF title: 20", "NOTIF banner: 25",
    "NOTIF text: 200", "Лимиты", "en", "ru",
])
ws_limits.append(["greeting", "some brief", "", "", "", "", "Hello", "Привет"])
buf_limits = io.BytesIO()
wb_limits.save(buf_limits)
parsed_limits = parse_workbook(buf_limits.getvalue())
assert len(parsed_limits) == 1, parsed_limits
sheet_limits = parsed_limits[0]
assert sheet_limits["unrecognized_columns"] == [], sheet_limits["unrecognized_columns"]
assert set(sheet_limits["languages"]) == {"en", "ru"}, sheet_limits["languages"]
print("[OK] parse_workbook: per-channel character-limit columns (\"label: number\"), a bare "
      "«Лимиты» header, and «ТЗ» are dropped silently rather than flagged as unrecognized languages")

# --- register (tone of address) is no longer a document-gated pass/fail
# check at all (removed 2026-09-16, at Александр's explicit request — see
# models.Project's docstring): the old formal/informal-rule design could
# be structurally blind to a translator using the SAME wrong register in
# every single row (internally consistent, just consistently wrong — a
# check that only looks for disagreement between rows can never catch
# that). Replaced with a plain factual report of what's actually there,
# which the manager reads and judges for themselves. ---
from app.claude_client import (
    NO_REGISTER_DISTINCTION_LANGS,
    REGISTER_VALUE_TYPE,
    _checks_description,
    _lacks_register_distinction,
    _register_array_note,
    _register_instructions,
    build_register_report,
)

# _checks_description itself is now completely unaware of "register" —
# it never appears in the "Что проверять" problem list.
assert _checks_description(["register"]) is None, _checks_description(["register"])
assert "регистр" not in (_checks_description(["register", "typo"]) or "").lower()
print("[OK] _checks_description no longer mentions register at all — it's not an error-finding "
      "check any more, so it never appears in the \"Что проверять\" problem list")

# _register_instructions is the ENTIRE register-related prompt content now
# — empty when register isn't selected, and never framed as a problem to
# avoid or a mistake to flag when it is.
assert _register_instructions(["typo"], batch=True) == ""
batch_instr = _register_instructions(["register"], batch=True)
assert REGISTER_VALUE_TYPE in batch_instr, batch_instr
assert "НЕ находка об ошибке" in batch_instr, batch_instr
assert '"row"' in batch_instr, batch_instr  # batch mode tags entries by row number
single_instr = _register_instructions(["register"], batch=False)
assert REGISTER_VALUE_TYPE in single_instr, single_instr
assert '"row"' not in single_instr, single_instr  # single mode has exactly one pair, no row numbers
assert _register_array_note(["typo"]) == ""
assert _register_array_note(["register"]) != ""
print("[OK] _register_instructions carries the entire register task now (empty when not selected, "
      "explicitly framed as information-gathering rather than error-detection), and only the batch "
      "(multi-row) prompt tags entries by row number")

# Changed 2026-09-18 (Александр's ask, cutting cost on the pricier output
# side): a pair with no direct address at all no longer gets tagged
# "neutral" — it's skipped entirely, no entry at all. "neutral" must no
# longer be offered as a value option in either prompt variant, and both
# must instead tell the model to just omit the entry.
assert "neutral" not in batch_instr, batch_instr
assert "neutral" not in single_instr, single_instr
assert "не добавляй" in batch_instr.lower(), batch_instr
assert "не добавляй" in single_instr.lower(), single_instr
print("[OK] register instructions no longer offer \"neutral\" as a value — a pair/row with no direct "
      "address is skipped entirely (no entry at all) instead of costing a full JSON entry to say so")

# For a language with no grammatical formal/informal distinction at all
# (English's single "you" — Александр's own example, 2026-09-17), the
# register instructions/array-note are skipped ENTIRELY regardless of
# region ("en-us"/"en-gb"), so no register_value entries are ever asked
# for, and no register report is ever built for that language — not even
# the old "couldn't determine" fallback line, since the model is never
# asked in the first place. A language with a real distinction (Russian,
# German, ...) is completely unaffected.
assert _lacks_register_distinction("en") and _lacks_register_distinction("en-US") and _lacks_register_distinction("EN-gb")
assert not _lacks_register_distinction("ru") and not _lacks_register_distinction("de")
assert _register_instructions(["register"], batch=True, target_lang="en") == ""
assert _register_instructions(["register"], batch=False, target_lang="en-us") == ""
assert _register_array_note(["register"], target_lang="en") == ""
# ...but still fully asked for when no target_lang is given at all (the
# language-blind call shape every pre-existing test above already uses),
# and for any language not in the deliberately small NO_REGISTER_DISTINCTION_LANGS set.
assert _register_instructions(["register"], batch=True) != ""
assert _register_instructions(["register"], batch=True, target_lang="de") != ""
assert NO_REGISTER_DISTINCTION_LANGS == {"en"}, NO_REGISTER_DISTINCTION_LANGS
print("[OK] register instructions are skipped entirely for a language with no formal/informal "
      "distinction at all (English) — no register_value entries are ever requested for it, so no "
      "register report (not even a \"couldn't determine\" fallback) is ever built for that language")

# Александр's case, 2026-09-17: a real pt-BR check reported "everywhere on
# «вы»" for a document that actually used "você" throughout — the standard,
# default INFORMAL address in Brazilian Portuguese despite its
# third-person-looking conjugation (a mistake the model made by pattern-
# matching it against Spanish "usted"/French "vous", where that shape
# really is formal). _register_instructions now appends a short correcting
# hint for pt-BR specifically, in both batch and single mode; every other
# language (including plain "pt" with no region, and hypothetically "pt-pt"
# even though European Portuguese isn't in the map) is untouched.
pt_br_batch = _register_instructions(["register"], batch=True, target_lang="pt-br")
assert "você" in pt_br_batch and "o senhor" in pt_br_batch, pt_br_batch
pt_br_single = _register_instructions(["register"], batch=False, target_lang="pt-br")
assert "você" in pt_br_single and "o senhor" in pt_br_single, pt_br_single
assert "você" not in _register_instructions(["register"], batch=True, target_lang="ru")
assert "você" not in _register_instructions(["register"], batch=True, target_lang="pt")
assert "você" not in _register_instructions(["register"], batch=True, target_lang="pt-pt")
assert "você" not in _register_instructions(["register"], batch=True)
# The standalone /check endpoint passes target_lang through raw (never
# normalized to hyphen form), and a manager-taught alias isn't required to
# use a hyphen either — an underscore spelling must still match.
assert "você" in _register_instructions(["register"], batch=True, target_lang="PT_BR")
print("[OK] register instructions carry a correcting hint for Brazilian Portuguese (pt-BR) specifically "
      "— \"você\" is the ordinary informal address there despite its third-person-shaped verb, not a "
      "formal marker — while every other language/region (plain \"pt\", \"pt-pt\", or no language at all) "
      "is completely unaffected")

# build_register_report: the actual Russian summary the manager reads,
# now a structured dict (not a plain string) so a caller can colorize
# «вы»/«ты» and, for a handful of exceptions, show the actual wrongly-toned
# text instead of just a row number (Александр's ask, 2026-09-17). This is
# the function his original use case hinges on — his report was five
# languages where EVERY row used the same wrong register, which is exactly
# the "everywhere, no exceptions" case below.
assert build_register_report({}) is None
everywhere_formal = build_register_report({5: "formal", 12: "formal"})
assert everywhere_formal == {"text": "Вы", "majority": "formal", "exceptions": None, "exception_labels": None}, everywhere_formal
everywhere_informal = build_register_report({1: "informal", 2: "informal", 3: "informal"})
assert everywhere_informal["text"] == "ты" and everywhere_informal["majority"] == "informal"
# a genuine minority gets called out by row number in `text` either way —
# and, when `texts` is given and there are few enough exceptions (<=3),
# ALSO gets each exception's actual text in `exceptions` instead of `exception_labels`
mixed = build_register_report({1: "formal", 2: "formal", 3: "formal", 5: "informal", 12: "informal"})
assert mixed["text"] == "Вы, кроме: строки 5, 12", mixed
assert mixed["majority"] == "formal"
assert mixed["exceptions"] is None and mixed["exception_labels"] == [5, 12], mixed  # no `texts` given here
mixed_with_texts = build_register_report(
    {1: "formal", 2: "formal", 3: "formal", 5: "informal", 12: "informal"},
    {1: "Hi", 2: "Hi", 3: "Hi", 5: "wrong tone here", 12: "and here too"},
)
assert mixed_with_texts["exception_labels"] is None, mixed_with_texts
assert mixed_with_texts["exceptions"] == [
    {"label": 5, "text": "wrong tone here"}, {"label": 12, "text": "and here too"},
], mixed_with_texts
# more than 3 exceptions falls back to plain row numbers even WITH texts —
# Александр's own cutoff ("если строк ... более трёх, то лучше номера").
# Majority (formal) must outnumber the exceptions (informal) for "formal"
# to actually win the majority — 5 formal rows vs. 4 informal ones.
many_exceptions = build_register_report(
    {1: "formal", 2: "formal", 3: "formal", 4: "formal", 5: "formal",
     6: "informal", 7: "informal", 8: "informal", 9: "informal"},
    {1: "Hi", 2: "Hi", 3: "Hi", 4: "Hi", 5: "Hi", 6: "a", 7: "b", 8: "c", 9: "d"},
)
assert many_exceptions["exceptions"] is None, many_exceptions
assert many_exceptions["exception_labels"] == [6, 7, 8, 9], many_exceptions
# a SINGLE exception uses the singular "строка", not "строки" ("кроме:
# строки 3" reads as a grammar mistake to a Russian speaker)
single_exc = build_register_report({1: "formal", 2: "formal", 3: "informal"})
assert single_exc["text"] == "Вы, кроме: строка 3", single_exc
# "neutral" rows (no direct address at all — a title, a number) are excluded
# from the count entirely, not treated as a third camp
assert build_register_report({1: "formal", 2: "neutral", 3: "formal"})["text"] == "Вы"
# nothing classifiable at all -> nothing to report at all any more (changed
# 2026-09-18 — used to be an explicit "не удалось определить" placeholder,
# now simply None, same as if register hadn't been asked for)
assert build_register_report({1: "neutral", 2: "neutral"}) is None
# single-pair mode drops the "везде"/"кроме" framing entirely (nothing to
# compare a lone pair against) and never has exceptions — just the bare word
single_formal = build_register_report({0: "formal"}, single=True)
assert single_formal == {"text": "Вы", "majority": "formal", "exceptions": None, "exception_labels": None}
assert build_register_report({0: "informal"}, single=True)["text"] == "ты"
print("[OK] build_register_report: everywhere-the-same reports just the bare word (\"Вы\"/\"ты\"), a "
      "genuine minority is called out by row number (or, for <=3 exceptions with texts given, by the "
      "actual wrongly-toned text), rows with no direct address are excluded from the count rather than "
      "treated as a third camp, nothing classifiable at all now means no report at all (not a placeholder "
      "sentence), and single-pair mode drops the \"везде\"/\"кроме\" framing")

# --- end-to-end: the standalone /check endpoint actually produces a
# "register_summary" finding from a mocked AI response, and — critically —
# the raw register_value entry itself never leaks into the visible
# findings (it's not a real problem, so it must never look like one) ---
async def _fake_call_claude_register_single(prompt, model=None):
    assert REGISTER_VALUE_TYPE in prompt
    return (
        f'[{{"type": "{REGISTER_VALUE_TYPE}", "severity": "low", "value": "formal", "message": ""}}]',
        {"input_tokens": 10, "output_tokens": 10},
        "end_turn",
    )


settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
claude_client_mod._call_claude = _fake_call_claude_register_single
r = check("register-only /check produces a register_summary, not a raw register_value", client.post(
    "/check", json={"source": "Play now.", "translation": "Играйте сейчас.", "checks": ["register"]},
))
reg_findings = r.json()["findings"]
assert len(reg_findings) == 1, reg_findings
assert reg_findings[0]["type"] == "register_summary", reg_findings
assert reg_findings[0]["message"] == "Тон: Вы.", reg_findings
assert reg_findings[0]["register_majority"] == "formal", reg_findings
claude_client_mod._call_claude = _previous_call_claude
settings.ANTHROPIC_API_KEY = ""
print("[OK] standalone /check with only \"register\" selected turns a mocked AI response into a "
      "single register_summary finding — the raw register_value entry never reaches the visible "
      "findings list")

# --- for a language with no formal/informal distinction (English), a
# register-only /check must not call the AI AT ALL — a poison mock that
# raises if invoked proves it, rather than just checking the output looks
# right (which a lucky no-op response could also produce). ---
async def _poison_call_claude(prompt, model=None):
    raise AssertionError("the AI must never be called for a register-only check on a language with "
                          "no formal/informal distinction (English) — nothing to ask it")


settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
claude_client_mod._call_claude = _poison_call_claude
r = check("register-only /check on English (no ты/вы distinction) never calls the AI at all", client.post(
    "/check", json={"source": "Play now.", "translation": "Play now.", "checks": ["register"], "target_lang": "en"},
))
assert r.json()["findings"] == [], r.json()
assert r.json()["cost_usd"] == 0.0, r.json()
claude_client_mod._call_claude = _previous_call_claude
settings.ANTHROPIC_API_KEY = ""
print("[OK] a register-only check against English (no formal/informal distinction) skips the AI call "
      "entirely — no register report is built (not even a \"couldn't determine\" fallback), and no "
      "cost is incurred for asking a question the language has no real answer to")

# --- same wiring, but for a multi-check FILE upload's live (synchronous)
# path — _check_language_for_sheet, the one place that actually knows the
# excel_row for each checked row, is what turns the model's per-"row"
# register_value entries into the "кроме: строки ..." exception list. Three
# rows, one deliberately different from the other two, so the summary must
# name exactly that row and no others. ---
from app.excel_multi import _check_language_for_sheet

_reg_sheet = {
    "sheet_name": "Sheet1",
    "languages": ["en", "ru"],
    "rows": [
        {"excel_row": 2, "context": "greeting", "max_length": None, "values": {"en": "Hello", "ru": "Привет"}},
        {"excel_row": 3, "context": "farewell", "max_length": None, "values": {"en": "Bye", "ru": "Здарова, пока"}},
        {"excel_row": 5, "context": "welcome", "max_length": None, "values": {"en": "Welcome", "ru": "Добро пожаловать"}},
    ],
}


async def _fake_call_claude_register_batch(prompt, model=None):
    assert REGISTER_VALUE_TYPE in prompt
    return (
        '[{"row": 1, "type": "register_value", "severity": "low", "value": "formal", "message": ""},'
        '{"row": 2, "type": "register_value", "severity": "low", "value": "informal", "message": ""},'
        '{"row": 3, "type": "register_value", "severity": "low", "value": "formal", "message": ""}]',
        {"input_tokens": 30, "output_tokens": 30},
        "end_turn",
    )


settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
claude_client_mod._call_claude = _fake_call_claude_register_batch
_reg_out, _reg_cost = asyncio.get_event_loop().run_until_complete(
    _check_language_for_sheet(_reg_sheet, "ru", "en", ["register"], "", asyncio.Semaphore(5))
)
claude_client_mod._call_claude = _previous_call_claude
settings.ANTHROPIC_API_KEY = ""
assert len(_reg_out) == 1, _reg_out  # nothing else had a real finding — just the one summary row
_reg_finding = _reg_out[0]["findings"][0]
assert _reg_finding["type"] == "register_summary", _reg_out
assert _reg_finding["message"] == "Тон: Вы, кроме: строка 3.", _reg_out
assert _reg_finding["register_majority"] == "formal", _reg_finding
# 1 exception (<=3) and the row's own translated text was available, so the
# structured fields carry the actual wrongly-toned text, not just the row
# number — see build_register_report/_register_summary_block.
assert _reg_finding["register_exceptions"] == [{"label": 3, "text": "Здарова, пока"}], _reg_finding
assert "register_exception_labels" not in _reg_finding, _reg_finding
assert not any(
    f.get("type") == REGISTER_VALUE_TYPE for row in _reg_out for f in row["findings"]
), "raw register_value must never reach the live multi-check path's visible findings"
print("[OK] multi-check live path (_check_language_for_sheet): a mocked per-row register_value "
      "response becomes one register_summary row naming the excel_row of the actual exception "
      "(3 — the row deliberately given a different register than the other two), carrying that row's "
      "actual translated text, with no raw register_value finding ever reaching the visible list")

# --- and the Message-Batches (large-file) path — finalize_batch_results,
# which merges a polled batch result back into the skeleton. Same
# register_value entries, reached via the async-batch machinery instead of
# an awaited call, must produce the identical summary. ---
from app.excel_multi import build_batch_plan, finalize_batch_results

_reg_requests, _reg_skeleton = build_batch_plan([_reg_sheet], "en", ["register"], "", None)
assert len(_reg_requests) == 1, _reg_requests
_reg_custom_id = _reg_requests[0]["custom_id"]
_reg_batch_results = {
    _reg_custom_id: {
        "text": (
            '[{"row": 1, "type": "register_value", "severity": "low", "value": "formal", "message": ""},'
            '{"row": 2, "type": "register_value", "severity": "low", "value": "informal", "message": ""},'
            '{"row": 3, "type": "register_value", "severity": "low", "value": "formal", "message": ""}]'
        ),
        "usage": {"input_tokens": 30, "output_tokens": 30},
        "result_type": "succeeded",
        "stop_reason": "end_turn",
    }
}
_reg_finalized = finalize_batch_results(_reg_skeleton, _reg_batch_results)
_reg_lang_findings = _reg_finalized["sheets"][0]["languages"]["ru"]
assert len(_reg_lang_findings) == 1, _reg_lang_findings
_reg_batch_finding = _reg_lang_findings[0]["findings"][0]
assert _reg_batch_finding["type"] == "register_summary", _reg_lang_findings
assert _reg_batch_finding["message"] == "Тон: Вы, кроме: строка 3.", _reg_lang_findings
assert _reg_batch_finding["register_majority"] == "formal", _reg_batch_finding
assert _reg_batch_finding["register_exceptions"] == [{"label": 3, "text": "Здарова, пока"}], _reg_batch_finding
assert "register_exception_labels" not in _reg_batch_finding, _reg_batch_finding
assert not any(
    f.get("type") == REGISTER_VALUE_TYPE for row in _reg_lang_findings for f in row["findings"]
), "raw register_value must never reach the Message-Batches path's visible findings either"
print("[OK] multi-check Message-Batches path (finalize_batch_results): the same polled register_value "
      "response produces the identical register_summary, with the same excel_row exception and its "
      "actual translated text, and no raw register_value finding reaching the visible list")

# --- "mixed": a row whose OWN translation switches between «ты» and «вы»
# within itself (a cell holding several sentences/paragraphs where the tone
# drifts mid-cell — Александр's ask, 2026-09-17) is a real problem on that
# specific row, not a document-wide majority question — it must show up as
# an ordinary visible finding (REGISTER_MIXED_TYPE), and must NOT be folded
# into build_register_report's majority/exception counting at all. ---
from app.claude_client import REGISTER_MIXED_TYPE


async def _fake_call_claude_register_mixed_single(prompt, model=None):
    return (
        f'[{{"type": "{REGISTER_VALUE_TYPE}", "severity": "low", "value": "mixed", "message": ""}}]',
        {"input_tokens": 10, "output_tokens": 10},
        "end_turn",
    )


settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
claude_client_mod._call_claude = _fake_call_claude_register_mixed_single
r = check("standalone /check turns a mocked \"mixed\" register_value into a register_mixed finding", client.post(
    "/check", json={"source": "Please confirm. Ты не против?", "translation": "Please confirm. Ты не против?", "checks": ["register"]},
))
mixed_findings = r.json()["findings"]
assert len(mixed_findings) == 1, mixed_findings
assert mixed_findings[0]["type"] == REGISTER_MIXED_TYPE, mixed_findings
assert "mixed" not in mixed_findings[0]["message"], mixed_findings  # a real Russian message, not a raw code
claude_client_mod._call_claude = _previous_call_claude
settings.ANTHROPIC_API_KEY = ""
print("[OK] standalone /check: a single pair the model reports as internally mixed produces one plain "
      "register_mixed finding instead of a register_summary — never a raw register_value leaking through")

# Same thing on the multi-check live path, alongside two ordinary rows —
# proves the mixed row (excel_row 3) both surfaces its own finding AND is
# excluded entirely from the OTHER rows' majority/exception calculation
# (both remaining rows are "formal", so the summary must read plainly
# "везде на «вы»" with no exceptions at all, as if row 3 didn't exist for
# that purpose).
async def _fake_call_claude_register_mixed_batch(prompt, model=None):
    return (
        '[{"row": 1, "type": "register_value", "severity": "low", "value": "formal", "message": ""},'
        '{"row": 2, "type": "register_value", "severity": "low", "value": "mixed", "message": ""},'
        '{"row": 3, "type": "register_value", "severity": "low", "value": "formal", "message": ""}]',
        {"input_tokens": 30, "output_tokens": 30},
        "end_turn",
    )


settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
claude_client_mod._call_claude = _fake_call_claude_register_mixed_batch
_mixed_out, _mixed_cost = asyncio.get_event_loop().run_until_complete(
    _check_language_for_sheet(_reg_sheet, "ru", "en", ["register"], "", asyncio.Semaphore(5))
)
claude_client_mod._call_claude = _previous_call_claude
settings.ANTHROPIC_API_KEY = ""
_mixed_rows_by_excel_row = {row["excel_row"]: row for row in _mixed_out}
assert 3 in _mixed_rows_by_excel_row, _mixed_out  # the mixed row itself must show up with its own finding
assert _mixed_rows_by_excel_row[3]["findings"][0]["type"] == REGISTER_MIXED_TYPE, _mixed_rows_by_excel_row[3]
_mixed_summary = next(row for row in _mixed_out if row["findings"][0]["type"] == "register_summary")
assert _mixed_summary["findings"][0]["message"] == "Тон: Вы.", _mixed_summary
assert _mixed_summary["findings"][0]["register_majority"] == "formal", _mixed_summary
assert "register_exceptions" not in _mixed_summary["findings"][0], _mixed_summary
assert "register_exception_labels" not in _mixed_summary["findings"][0], _mixed_summary
print("[OK] multi-check live path: a row the model reports as internally mixed gets its own "
      "register_mixed finding, and is excluded entirely from the other rows' majority/exception "
      "calculation — not counted as a vote for the majority and not counted as an exception either")

# _count_real_findings: the "N проблем"/"N найдено" number shown across the
# UI must never count the register_summary report as a problem — a check
# that only ran "register" on an otherwise-clean document should say 0
# problems, not 1 per language, or the whole point of dropping the old
# pass/fail tone judgment is undone by the aggregate count alone.
from app.excel_multi import _count_real_findings

assert _count_real_findings([]) == 0
assert _count_real_findings([{"findings": [{"type": "register_summary", "message": "..."}]}]) == 0
assert _count_real_findings([
    {"findings": [{"type": "typo", "message": "..."}]},
    {"findings": [{"type": "register_summary", "message": "..."}]},
]) == 1
# a real finding riding alongside a register_summary row in the very same
# row (shouldn't normally happen — they're separate synthetic rows — but
# the count must still only drop the register_summary entry, not the row)
assert _count_real_findings([
    {"findings": [{"type": "typo", "message": "..."}, {"type": "register_summary", "message": "..."}]},
]) == 1
# truncation/AI-failure warnings ("system") are deliberately still counted
# — those genuinely are something to notice, unlike the register report
assert _count_real_findings([{"findings": [{"type": "system", "message": "..."}]}]) == 1
# register_mixed (a row internally switching «ты»/«вы») is a genuine
# problem, unlike register_summary — it must count normally.
assert _count_real_findings([{"findings": [{"type": REGISTER_MIXED_TYPE, "message": "..."}]}]) == 1
print("[OK] _count_real_findings excludes the synthetic register_summary report from the \"N problems\" "
      "count everywhere it's used, while still counting real findings, system warnings, and "
      "register_mixed findings")

# --- letter-run placeholder + grammatical suffix glued on with no space
# (Александр's ask, 2026-09-22): Korean/Turkic-style agglutination attaches
# a case/particle ending directly onto a letter-run placeholder with no
# separating space. The old regex required a trailing \b, and Unicode word
# characters (Hangul included) never form a boundary against the
# placeholder's own Latin letters, so the run went completely unmatched in
# the translation and was reported as "lost" even though it's genuinely
# there.
from app.rule_checks import (
    check_em_dash_spacing,
    check_hyphen_for_dash,
    check_letter_placeholders,
    check_punctuation,
)

korean_src = (
    "A specialized collection of games by XXXXXXXXXXXXXXXXXXXX is now accessible on your account."
)
korean_tr = "XXXXXXXXXXXXXXXXXXXX의 엄선된 게임 컬렉션을 이제 계정에서 이용할 수 있습니다."
assert check_letter_placeholders(korean_src, korean_tr) == [], (
    "a letter-run placeholder with a grammatical suffix glued directly onto it (no space) must still be "
    "recognized as present, not reported as lost"
)
# a Turkic-style possessive/case suffix glued the same way
assert check_letter_placeholders("Your code: XXXXXXXX", "Kodunuz: XXXXXXXXniz") == [], (
    "the same glued-suffix tolerance must hold for a Latin-script agglutinative suffix, not just Hangul"
)
# genuinely dropped placeholder must still be caught even with a suffix language in play
assert check_letter_placeholders("Your code: XXXXXXXX", "Kodunuz hazır") != [], (
    "a placeholder actually missing from the translation must still be flagged — the suffix fix must not "
    "make the check blind to real drops"
)
print("[OK] check_letter_placeholders: a grammatical suffix glued directly onto a letter-run placeholder "
      "with no space (Korean, Turkic, ...) no longer produces a false \"placeholder lost\" finding, while a "
      "genuinely dropped placeholder is still caught")

# Regression caught in review of the first version of this fix: loosening
# _PLACEHOLDER_LETTER_RUN_RE itself (dropping its trailing \b) also made it
# match the first 4+ letters of any ordinary word that happens to start
# with a repeated letter and continue as the same word — an informal
# elongated-emphasis spelling ("оооочень"), or a marketing "WOOOOW". Fixed
# by leaving the regex exactly as strict as before and instead doing a
# plain, boundary-free substring check for the specific already-known
# source text — these must stay clean.
assert check_letter_placeholders("This offer is amazing!", "Это оооочень крутое предложение!") == [], (
    "an ordinary word that happens to start with 4+ of the same letter (informal emphasis) must not be "
    "misread as a letter-run placeholder"
)
assert check_letter_placeholders("Wow, big win!", "WOOOOW, большой выигрыш!") == [], (
    "an all-caps elongated exclamation (\"WOOOOW\") must not be misread as a letter-run placeholder either"
)
print("[OK] check_letter_placeholders: the glued-suffix fix does not loosen the underlying regex, so an "
      "ordinary word starting with a repeated letter (informal emphasis, \"WOOOOW\"-style exclamations) is "
      "still correctly left alone")

# Regression caught in review: the glued-suffix survival check must not
# credit the SAME physical occurrence of a repeated placeholder text to
# more than one leftover entry — a naive "is this text anywhere in the
# translation" test would let a surviving copy silently mask a genuinely
# DIFFERENT, separately-dropped occurrence of the identical placeholder
# (e.g. two masked card numbers both shown as "XXXXXXXX").
dup_one_dropped = check_letter_placeholders(
    "Card 1: XXXXXXXX. Card 2: XXXXXXXX.", "Karta 1: XXXXXXXX. Karta 2 nomeri yashirin."
)
assert dup_one_dropped != [], (
    "when the same placeholder text appears twice in the source and only one copy survives in the "
    "translation, the genuinely dropped second copy must still be flagged"
)
assert check_letter_placeholders(
    "Card 1: XXXXXXXX. Card 2: XXXXXXXX.", "Karta1: XXXXXXXXbb. Karta2: XXXXXXXXcc."
) == [], "but when BOTH copies survive, each with its own glued suffix, neither should be flagged"
print("[OK] check_letter_placeholders: the glued-suffix survival check correctly tracks each occurrence of a "
      "repeated placeholder text separately, instead of one surviving copy masking a different dropped one")

# Regression caught in review: counting "glued survivors" with a plain
# text.count(run) can't tell a genuine SHORT placeholder from that many
# letters sitting INSIDE an unrelated LONGER run of the same letter — a
# surviving 16-letter masked card number textually contains every possible
# 4-letter substring of "X", so a bare substring count wrongly credited a
# completely different, genuinely-dropped 4-letter PIN placeholder as
# still present.
diff_length_dropped = check_letter_placeholders(
    "Card: XXXXXXXXXXXXXXXX. PIN: XXXX.", "Karta: XXXXXXXXXXXXXXXX. PIN otsutstvuet."
)
assert diff_length_dropped != [], (
    "a genuinely dropped SHORT placeholder must still be flagged even when a longer run of the identical "
    "letter survives elsewhere in the same translation"
)
assert check_letter_placeholders(
    "Card: XXXXXXXXXXXXXXXX. PIN: XXXX.", "Karta: XXXXXXXXXXXXXXXX. PIN: XXXXqo."
) == [], "but when the short placeholder genuinely survives too (glued), it must not be flagged"
print("[OK] check_letter_placeholders: the glued-suffix survival check counts MAXIMAL same-length runs, not "
      "plain substrings, so a shorter placeholder's genuine loss isn't masked by an unrelated longer run of "
      "the same letter surviving elsewhere")

# --- check_punctuation: bidirectional terminal-punctuation detection, past
# trailing wrappers (Александр's ask, 2026-09-22: "не всегда видит
# присутствие/отсутствие... знака"). Two real bugs confirmed empirically
# and fixed: (1) translation ADDING an unwarranted terminal mark when the
# source has none was never checked at all; (2) naive src[-1]/tr[-1]
# indexing missed a genuinely dropped mark whenever it was followed by a
# trailing tag/placeholder/closing quote.
assert any(f["type"] == "punctuation" for f in check_punctuation("Play now", "Играйте сейчас.")), (
    "translation adding an unwarranted terminal period when the source has none at all must be flagged"
)
assert check_punctuation("Играйте сейчас.", "Play now.") == [], (
    "sanity: check_punctuation must still be callable with no checks= argument at all (existing call sites)"
)
assert check_punctuation("Click here.</b>", "Нажмите здесь</b>") != [], (
    "a genuinely dropped terminal period must still be caught even when the real last character is followed "
    "by a trailing closing tag"
)
assert check_punctuation("Click here.</b>", "Нажмите здесь.</b>") == [], (
    "a correctly preserved terminal period followed by a trailing closing tag must NOT be flagged as missing"
)
assert check_punctuation("Order now!", "Закажите сейчас!") == [], (
    "matching terminal punctuation must not be flagged"
)
print("[OK] check_punctuation: now catches a translation ADDING an unwarranted terminal mark when the source "
      "has none (not just dropping one), and correctly looks past a trailing closing tag/placeholder/quote "
      "instead of naively indexing the very last character")

# Regression caught in review: _real_last_char used PLACEHOLDER_RE.search()
# alone, which returns the LEFTMOST match in the string, not one anchored
# at the end — a string with an earlier tag too ("<b>...</b> text.<icon>")
# never had that leftmost match reach the end, so the real trailing token
# was never stripped and this fell back to naive last-character indexing,
# the exact bug the rewrite was meant to fix. Fixed by scanning every
# match (finditer) for the one actually touching the end of the string.
assert check_punctuation("Claim your bonus now.", "<b>Заберите</b> бонус сейчас.<icon>") == [], (
    "a correctly preserved terminal period must not be flagged as missing just because an EARLIER tag/"
    "placeholder appears before the real trailing one in the same string"
)
# Regression caught in review: an empty _real_last_char() result (source or
# translation is nothing but a stripped-away placeholder) used to pass
# straight into Python's `"" in some_string`, which is always True —
# silently mis-firing both the forward and reverse checks. Both directions
# must now behave sanely when there's no real trailing character at all.
assert check_punctuation("{icon}", "Иконка бонуса.") == [], (
    "a source that's nothing but a placeholder must not spuriously trigger the terminal-punctuation checks"
)
assert check_punctuation("{icon}", "{icon}") == [], (
    "a source and translation that are both nothing but a placeholder must not produce a garbled empty-"
    "character punctuation finding"
)
print("[OK] check_punctuation: _real_last_char correctly finds the trailing tag/placeholder even when an "
      "earlier one appears first in the same string, and a placeholder-only source or translation (no real "
      "trailing character at all) no longer produces a spurious or garbled finding")

# Regression caught in review: _real_last_char's wrapper-stripping loop
# used to be unbounded ("while changed"), re-scanning the whole shrinking
# string every layer — quadratic for a pathological string built almost
# entirely of tiny trailing wrapper tokens. Now capped at a fixed number
# of layers (_MAX_WRAPPER_STRIP_LAYERS), which real content never gets
# close to, but a corrupted/malformed cell can't turn into a slow check.
from app.rule_checks import _real_last_char
import time as _time

_perf_start = _time.time()
_real_last_char("text" + "<i></i>" * 20000)
assert _time.time() - _perf_start < 2.0, (
    "a pathological string with many tiny trailing wrapper tokens must not make _real_last_char slow"
)
# a normal, realistic handful of stacked wrapper layers must still be fully unwrapped
assert _real_last_char("Great offer!</b>)") == "!", (
    "several ordinary trailing wrapper layers (closing tag, then closing bracket) must all be stripped to "
    "find the real terminal punctuation"
)
print("[OK] _real_last_char: bounded wrapper-stripping stays fast even on a pathological string with many "
      "tiny trailing tokens, while still fully unwrapping any realistic number of stacked layers")

# --- new "Оформление" rules: em dash needs spaces on both sides, and a
# hyphen standing in for a dash is flagged by meaning (Александр's ask,
# 2026-09-22), with an SMS/Latin exception since Александр's own SMS spec
# requires the opposite (plain hyphen, never an em dash).
assert check_em_dash_spacing("Быстро—просто.") != [], "an em dash glued to text with no space must be flagged"
assert check_em_dash_spacing("Быстро — просто.") == [], "a correctly spaced em dash must not be flagged"
assert check_hyphen_for_dash("Быстро - просто.") != [], (
    "a hyphen standing alone between spaces (typographically a dash, not a real hyphen) must be flagged"
)
assert check_hyphen_for_dash("видео-игра проста.") == [], (
    "an ordinary hyphenated compound word (no spaces around the hyphen) must not be flagged"
)
# folded into check_punctuation under the "punctuation" checkbox
assert any(f["type"] == "punctuation" for f in check_punctuation("Fast.", "Быстро—просто.")), (
    "the em-dash-spacing rule must be reachable through check_punctuation itself, not just its own function"
)
# SMS exception: skipped entirely when "sms_charset" is among the active checks
assert check_punctuation("Fast.", "Быстро - просто.", checks=["punctuation"]) != [], (
    "outside SMS, a hyphen standing in for a dash must be flagged"
)
assert check_punctuation("Fast.", "Быстро - просто.", checks=["punctuation", "sms_charset"]) == [], (
    "for SMS content, the dash-vs-hyphen and em-dash-spacing rules must be skipped entirely — Александр's own "
    "SMS spec requires a plain ASCII hyphen and forbids the em dash outright"
)
print("[OK] check_punctuation: new rules require spaces around an em dash and flag a lone hyphen standing in "
      "for one, both skipped for SMS content (where a plain hyphen is required and an em dash is forbidden)")

# Real bug caught live in production (Александр's report, 2026-09-22): a
# bulleted rules/T&C list flattened into one cell uses "- " as a list
# marker after a colon/semicolon/period, and the first version of
# check_hyphen_for_dash flagged every single bullet as a dash typo. Fixed
# by only flagging a hyphen whose preceding non-whitespace character is
# ordinary text, never one that just closed the previous clause/bullet
# (colon, semicolon, period, question/exclamation mark, line break, or the
# very start of the string).
from app.rule_checks import check_hyphen_for_dash

bulleted_rules = (
    "以下情况不发放免费赌注： - 使用奖励账户进行的赌注； - 使用免费赌注进行的赌注； - 退还的赌注； - 在结算前出售的赌注。"
)
assert check_hyphen_for_dash(bulleted_rules) == [], (
    "a bulleted rules list using \"- \" as a list marker after a colon/semicolon must NOT be flagged as a "
    "dash typo — this is the exact real-world text that surfaced the bug in production"
)
assert check_hyphen_for_dash("- First item. - Second item.") == [], (
    "a bullet at the very start of the string (nothing before it at all) must also not be flagged"
)
assert check_hyphen_for_dash("Играйте сейчас - выигрывайте призы.") != [], (
    "a genuine mid-sentence dash typo (ordinary text right before the hyphen, not a clause-closing mark) "
    "must still be caught — the bullet-list fix must not silence real cases"
)
print("[OK] check_hyphen_for_dash: a bulleted list (\"...правила: - пункт один; - пункт два;\") is no longer "
      "misread as a run of dash typos, while a genuine mid-sentence \" - \" typo is still caught")

# Two more real gaps caught in a second round of review of the same fix:
# (a) excluding a hyphen whenever the single character right before it was
# clause-closing punctuation also silently missed a genuine dash typo that
# happens to open a NEW sentence/clause ("Ты гений! - воскликнул он." is a
# real typo, not a list); (b) a bulleted list whose OWN items aren't
# separated by semicolons/periods at all ("Не действует на: - ставки А -
# ставки Б - возврат.") still only had its very first bullet recognized —
# every later one was still flagged. Fixed with a "sentence-scoped colon,
# then sticky" rule: a hyphen is a list marker if a colon appears anywhere
# since the current sentence started (or the hyphen opens the whole cell),
# and once ONE hyphen in a cell is recognized as a bullet, every later one
# in the same cell is treated as continuing that list too.
assert check_hyphen_for_dash("Ты гений! - воскликнул он.") != [], (
    "a genuine dash typo that happens to open a new sentence/clause (dialogue attribution, no colon anywhere "
    "in that sentence) must still be caught, not swallowed by the bullet-list exception"
)
assert check_hyphen_for_dash(
    "Не действует на: - ставки со счета А - ставки со счета Б - возвращенные ставки."
) == [], (
    "every bullet in a colon-introduced list must be recognized as a list marker, even when the bullets "
    "themselves aren't separated by semicolons/periods — not just the very first one"
)
assert check_hyphen_for_dash("- Первый пункт. - Второй пункт.") == [], (
    "a list with no leading space before its very first bullet (so the extraction regex's own whitespace "
    "lookbehind can't even see that first hyphen) must still have its later bullets recognized as a list, "
    "not flagged as typos"
)
print("[OK] check_hyphen_for_dash: a genuine dash typo opening a new sentence is still caught (not swallowed "
      "by the bullet-list exception), and every bullet of a colon-introduced list is recognized — not just "
      "the first one — regardless of what (if anything) separates the bullets themselves")

# Three more real bugs caught in a third round of review of the same fix:
# (a) a Python negative-index pitfall — bisect_right(...) - 1 can come out
# to -1 (no sentence boundary at all before this hyphen yet), and
# indexing a non-empty list at -1 silently wraps to its LAST element
# instead of meaning "none found", which made the very first colon check
# in a cell use the LAST boundary in the whole string as if it were the
# current sentence's start — this alone reopened the original production
# bug (the exact colon+semicolon list was wrongly flagged again once the
# O(n²) rewrite introduced it); (b) a decimal point in an odds value
# ("1.5", "4.0" — extremely common in this gambling-promo content) was
# treated as a sentence-ending period, dropping an earlier list-opening
# colon out of scope; (c) an ellipsis "..." didn't register as a sentence
# boundary at all, letting a colon from an much earlier, unrelated
# sentence stay "in scope" across it and mask a genuine later dash typo.
translation_again = (
    "以下情况不发放免费赌注： - 使用奖励账户进行的赌注； - 使用免费赌注进行的赌注； - 退还的赌注； - 在结算前出售的赌注。"
)
assert check_hyphen_for_dash(translation_again) == [], (
    "the exact production bulleted-list text must still be recognized as a list after the performance "
    "rewrite — a negative-index bug in the rewrite silently reopened the original false positive"
)
assert check_hyphen_for_dash(
    "Не действует на: - ставки с коэф. менее 1.5 - ставки более 4.0."
) == [], "a decimal odds value (\"1.5\", \"4.0\") inside a colon-introduced list must not be mistaken for a " \
    "sentence-ending period and drop the list's own colon out of scope"
assert check_hyphen_for_dash("Услуга недоступна: подробности... Играй - выигрывай.") != [], (
    "an ellipsis must count as a real sentence boundary — a colon from an earlier, unrelated sentence must "
    "not stay \"in scope\" across it and mask a genuine later dash typo"
)
print("[OK] check_hyphen_for_dash: a negative-index bug that had silently reopened the original production "
      "false positive is fixed, a decimal odds value inside a list no longer breaks the list's own colon "
      "scope, and an ellipsis correctly ends a sentence instead of letting an unrelated earlier colon linger")

# Performance: the position-based rewrite must stay roughly linear, not
# quadratic, on a long cell with many hyphens (the flattened FAQ/T&C
# blocks this check exists for can run to tens of thousands of characters).
import time as _hyphen_perf_time

_perf_text = "Правила: " + " - пункт правила номер такой-то, с длинным текстом внутри" * 3000
_perf_start = _hyphen_perf_time.time()
check_hyphen_for_dash(_perf_text)
assert _hyphen_perf_time.time() - _perf_start < 2.0, (
    "check_hyphen_for_dash must stay fast on a long cell with many hyphens, not blow up quadratically"
)
print("[OK] check_hyphen_for_dash: stays fast on a long cell with many hyphens (position-based, not "
      "re-scanning the whole prefix for every single hyphen)")

# A fourth real gap caught in a fourth review round: an abbreviation
# period ("т.н.", "e.g.") isn't adjacent to a digit, so the decimal guard
# alone didn't stop it from being mistaken for a sentence-ending period,
# which dropped an earlier list-opening colon out of scope the same way
# the decimal bug did. Fixed by also requiring a genuine sentence-ending
# period to be followed by whitespace/end-of-string and NOT continue
# straight into a lowercase word.
assert check_hyphen_for_dash("Не действует на: т.н. бонусные игры - слоты - джекпоты.") == [], (
    "a Russian abbreviation (\"т.н.\") inside a colon-introduced list must not be mistaken for a "
    "sentence-ending period and drop the list's own colon out of scope"
)
assert check_hyphen_for_dash("Not valid for: e.g. bonus games - slots - jackpots.") == [], (
    "the same must hold for a Latin abbreviation (\"e.g.\")"
)
print("[OK] check_hyphen_for_dash: an abbreviation period (\"т.н.\", \"e.g.\") inside a colon-introduced list "
      "is no longer mistaken for a sentence-ending period")

# A fifth real gap caught in a fifth review round, and it was a genuine
# regex-backtracking trap, not just a missing case: the abbreviation fix
# above skipped a run of whitespace/closing-quote characters after a
# period before checking whether a lowercase letter followed — but the
# "skip" class and the "is this a lowercase letter" check class
# overlapped (both matched plain whitespace), so the engine could
# backtrack to skip ZERO characters and let the space itself satisfy
# "not a lowercase letter", short-circuiting past the real following word
# without ever reaching it. Concretely: a period followed by a closing
# quote mark before the actual sentence-ending space+capital-letter
# ('Он сказал: "Всё готово." Играй - выигрывай.') was wrongly excluded
# as a boundary, leaving the earlier colon "in scope" for the later,
# unrelated genuine typo. Fixed by making the "skip" and "terminator"
# character classes mutually exclusive so the skip can't be shortchanged.
assert check_hyphen_for_dash('Он сказал: "Всё готово." Играй - выигрывай.') != [], (
    "a sentence-ending period followed by a closing quote before the actual whitespace must still be "
    "recognized as ending the sentence, so an unrelated earlier colon doesn't stay \"in scope\" and mask a "
    "genuine later dash typo"
)
print("[OK] check_hyphen_for_dash: a period followed by a closing quote/bracket before the real whitespace "
      "is still correctly recognized as ending the sentence, not swallowed by a regex-backtracking gap in "
      "the abbreviation-detection logic")

# A sixth real gap, caught live on Александр's actual production content
# (a real casino free-bet promo cell, 2026-09-22): his bulleted lists are
# very often laid out with a BLANK LINE between the colon-introducing
# header and each bullet, and between the bullets themselves — purely as
# visual spacing within one Excel cell, not as separate "sentences". A
# bare newline was still one of the sentence-boundary characters at the
# time, so it reset the current-sentence scope right before every single
# bullet, dropping the list's own intro colon out of scope every time and
# reflagging the whole list all over again — even though the flat,
# no-newline version of the exact same list already worked correctly.
# Fixed by dropping the bare newline from the boundary set entirely (a
# real sentence-ending mark is already a strong enough signal on its own).
real_production_row = (
    "以下情况不发放免费赌注：\n\n"
    "- 使用奖励账户进行的赌注；\n\n"
    "- 使用免费赌注进行的赌注；\n\n"
    "- 退还的赌注；\n\n"
    "- 在结算前出售的赌注。"
)
assert check_hyphen_for_dash(real_production_row) == [], (
    "a colon-introduced bulleted list laid out with a blank line between the header and each bullet (real "
    "production content) must be recognized as a list, not reflagged as four separate dash typos"
)
assert check_hyphen_for_dash("Играй сейчас - выигрывай.\nПозже - смотри.") != [], (
    "two genuinely separate dash typos on different lines (no period between them) must still both be "
    "caught — dropping the newline boundary must not make this check blind to typos separated only by a "
    "line break"
)
print("[OK] check_hyphen_for_dash: a bulleted list laid out with a blank line between the colon header and "
      "each bullet (real production content) is correctly recognized as a list, while genuine typos "
      "separated only by a line break are still both caught")

# A real numbers-check false positive, also caught live on Александр's
# actual production content (the same 2026-09-22 casino free-bet promo
# cell): betting-odds ranges like "с коэффициентом от 1.25 до 4.0" are
# extremely common in his gambling/casino content, and "1.25" alone has
# exactly the same day.month shape as a genuine short date (both halves
# are 1-31) — so it was being silently split into '1'/'25' by the
# short-date decomposition, causing a spurious numbers-mismatch finding
# even though every target language carried the exact same odds range.
# Fixed with _near_range_partner: a real date is essentially never paired
# with a SECOND decimal-shaped number nearby, while a range always is.
odds_source = "Available on bets with odds from 1.25 to 4.0."
odds_translation_de = "Verfügbar bei Wetten mit Quoten von 1,25 bis 4,0."
assert check_numbers(odds_source, odds_translation_de) == [], (
    "a betting-odds decimal range ('1.25 to 4.0') must not be misread as a short date and split into "
    "separate digits — this must not produce a spurious numbers mismatch"
)
# Control: a genuine short date (only ONE decimal-shaped token nearby, no
# range partner) must still be freely reorderable/normalizable exactly as
# before this fix — _near_range_partner must not over-suppress real dates.
assert check_numbers("Promo runs until 20.09.", "Акция до 09.20.") == [], (
    "a genuine short date with no nearby range partner must still compare order-agnostically as before"
)
# Control: a real numbers mismatch on an isolated decimal (no range
# partner, no currency marker) must still be caught.
assert check_numbers("The price is 1.25.", "Цена составляет 1.35.") != [], (
    "an isolated decimal mismatch with no nearby range partner must still be flagged as a real difference"
)
print("[OK] check_numbers: a betting-odds decimal range ('1.25 to 4.0', real production content) is no "
      "longer misidentified as a short date and split into separate digits, while genuine short dates and "
      "genuine isolated decimal mismatches are still handled correctly")

# Independent subagent review of the fix above caught two further real gaps
# in its first version (a flat 15-character window around the candidate
# number): (1) a translation phrased more verbosely than the English source
# (very common for German/Russian) can push the actual range partner
# outside a small fixed window, reopening the exact bug just fixed; (2) the
# partner-detection regex only recognized a PERIOD as a decimal separator,
# so a comma-decimal translation's own genuine date could be judged to have
# "no partner nearby" (and get split) while the period-decimal source
# correctly found its partner (and didn't) — an asymmetry that produced a
# fresh spurious mismatch of its own. Fixed by scoping the partner search to
# the current sentence (reusing the same _SENTENCE_BOUNDARY_RE/bisect
# machinery already hardened for check_hyphen_for_dash) instead of a flat
# window, and by recognizing both "." and "," as decimal separators.
odds_source_verbose = (
    "Available on selected bets with odds starting from as low as 1.25 and going "
    "all the way up to 4.0 for select events."
)
odds_translation_verbose_de = (
    "Verfügbar bei ausgewählten Wetten mit Quoten von mindestens 1,25 bis hin zu "
    "4,0 für bestimmte Events."
)
assert check_numbers(odds_source_verbose, odds_translation_verbose_de) == [], (
    "an odds range phrased verbosely enough to push the two numbers more than 15 characters apart must "
    "still be recognized as a range, not reflagged as a short-date mismatch"
)
assert check_numbers(
    "Promo runs until 20.09. Odds range from 1.25 to 4.0.",
    "Акция до 20.09. Коэффициенты от 1,25 до 4,0.",
) == [], (
    "a comma-decimal odds range ('1,25 до 4,0') must be recognized as a range partner exactly like a "
    "period-decimal one — before this fix, only the period-decimal source found its partner and kept its "
    "date as one atom, while the comma-decimal translation found none and split its (identical) date into "
    "separate digits, producing a mismatch out of two literally identical dates"
)
# NOTE (accepted trade-off, same principle as the pre-existing currency-marker
# case): a date that shares a sentence with a range/currency partner forgoes
# order-agnostic date comparison entirely (both sides stay as one exact-match
# atom) — so if the SAME date is genuinely written in a different digit order
# between source and translation while sharing a sentence with a range, it
# still surfaces as a mismatch. This is rare (a date and a decimal range
# coexisting in one sentence at all is uncommon) and matches how the
# currency-marker case already behaves; making it fully order-agnostic AND
# context-aware would need real date parsing, not a text heuristic.
assert check_numbers(
    "Promo runs until 20.09. Odds range from 1.25 to 4.0.",
    "Акция до 09.20. Коэффициенты от 1,25 до 4,0.",
) != [], (
    "documenting the accepted trade-off: a date reordered between source/translation while sharing a "
    "sentence with a range partner is still flagged, since suppressing short-date treatment near a range "
    "partner means the date is compared as one exact atom, not order-agnostically"
)
print("[OK] check_numbers: _near_range_partner scopes its search to the current sentence (not a flat "
      "character window), so a verbosely-phrased odds range is still recognized no matter how far apart "
      "the two numbers land, and a comma-decimal range partner is recognized exactly like a period-decimal "
      "one")

# A THIRD real gap, caught by a second independent review pass: a range's
# other bound is very often written as a plain WHOLE number with no decimal
# point at all ("odds from 1.25 to 4", not "...to 4.0") — normal, common
# phrasing. _DECIMAL_TOKEN_RE alone never matches that bound, so "1.25" was
# judged to have no partner and split into ['1','25'] on the period-decimal
# source, while the comma-decimal translation's "1,25" — which bypasses this
# logic entirely regardless of context (see _decompose_grouped's early
# return) — stayed one atom, reproducing the original bug via that
# asymmetry. Fixed by also accepting a bare integer as a partner, but only
# within a much tighter window (a real range bound is essentially always
# immediately adjacent; an unrelated integer is common enough in ordinary
# text that a loose, sentence-wide search for it would misfire constantly).
assert check_numbers(
    "Odds from 1.25 to 4 on this match.",
    "Коэффициенты от 1,25 до 4 в этом матче.",
) == [], (
    "a whole-number range bound ('1.25 to 4', no decimal point on the second number) must still be "
    "recognized as a range partner for the decimal-shaped bound, not just a second decimal-shaped number"
)
assert check_numbers(
    "Odds from 4 to 1.25 on this match.",
    "Коэффициенты от 4 до 1,25 в этом матче.",
) == [], "the integer-bound partner must be recognized on either side of the decimal-shaped token, not just after it"
# Regression guard: the tight integer-partner window must NOT swallow the
# pre-existing "short date directly followed by a clock time" case just
# above ("Confirm by 09/20, 23:59" / "Подтверди до 20.09, 23:59") — a time
# like "23:59" sits well within the tight window of a comma-separated date,
# and a first version of this fix broke exactly that already-tested case by
# treating "23" as a stray range partner. Colon-adjacent digits are always
# a time/ratio component, never a bare range bound, so they're excluded.
assert check_numbers("Confirm by 09/20, 23:59", "Подтверди до 20.09, 23:59") == [], (
    "a short date immediately followed by a clock time must still compare order-agnostically — a "
    "colon-joined number ('23:59') must never be mistaken for an integer range partner"
)
print("[OK] check_numbers: a whole-number range bound ('1.25 to 4') is recognized via a tight-proximity "
      "integer partner check, without breaking the pre-existing short-date-followed-by-a-clock-time case "
      "(a colon-adjacent number is never mistaken for a range bound)")

# --- Duplicate language-header columns (e.g. two columns both headed "ru")
# — a real structural ambiguity found live on Александр's real production
# file (2026-09-22): "ru" appeared as both column C and column AA. Only
# one column's data can ever be used per row (see parse_workbook's
# col_letters_by_code), and before this fix that silent "rightmost column
# wins" choice was never surfaced anywhere. His own ask, verbatim: "если
# платформа видит, что источника 2 или более и сомневается какой
# правильный, пусть сообщит об этом". Covers: parse_workbook detecting and
# DEDUPLICATING the language (a duplicate used to make every caller that
# builds target_langs from sheet["languages"] check it TWICE — doubling
# both AI cost and findings, on top of the ambiguity itself), the live
# run_multi_check path warning about a duplicated TARGET language, a
# duplicated SOURCE language's warning appearing exactly once across the
# whole sheet (not once per target language, which would misleadingly
# inflate the "N found" headline), a clean file producing no such warning
# at all, and the large-file Message Batch path (build_batch_plan /
# finalize_batch_results) surfacing the identical warning. ---
from app.excel_multi import run_multi_check as _run_multi_check_direct
from app.excel_multi import build_batch_plan as _dup_build_batch_plan
from app.excel_multi import finalize_batch_results as _dup_finalize_batch_results

_dup_target_wb = openpyxl.Workbook()
_dup_target_ws = _dup_target_wb.active
_dup_target_ws.append(["Context", "en", "ru", "de", "ru"])
_dup_target_ws.append(["greeting", "Hello", "Privet-C", "Hallo", "Privet-E"])
_dup_target_buf = io.BytesIO()
_dup_target_wb.save(_dup_target_buf)
_dup_target_buf.seek(0)
_dup_target_sheets = _parse_workbook_direct(_dup_target_buf.read())

assert _dup_target_sheets[0]["languages"] == ["de", "en", "ru"], (
    "a duplicated language code must appear only ONCE in the sheet's languages list — before this fix it "
    "appeared twice, which made every caller building target_langs from it (run_multi_check, "
    "estimate_check_volume, build_batch_plan) check that language TWICE, doubling both its AI cost and its "
    "findings"
)
assert _dup_target_sheets[0]["duplicate_language_columns"] == {"ru": ["C", "E"]}, (
    "parse_workbook must record which columns a duplicated language code came from, in left-to-right order"
)
assert _dup_target_sheets[0]["rows"][0]["values"]["ru"] == "Privet-E", (
    "the rightmost duplicate column's data is what's actually used per row — the warning message must "
    "describe this exact, real behavior, not a hypothetical one"
)

_dup_target_result = asyncio.run(_run_multi_check_direct(_dup_target_sheets, "en", ["numbers"]))
_dup_target_ru_findings = _dup_target_result["sheets"][0]["languages"]["ru"]
_dup_target_warnings = [f for row in _dup_target_ru_findings if row["excel_row"] == 0 for f in row["findings"]]
assert (
    len(_dup_target_warnings) == 1
    and "ru" in _dup_target_warnings[0]["message"]
    and "C" in _dup_target_warnings[0]["message"]
    and "E" in _dup_target_warnings[0]["message"]
), "a duplicated TARGET language column must produce exactly one visible system warning naming both columns"
_dup_target_de_findings = _dup_target_result["sheets"][0]["languages"]["de"]
assert not any(row["excel_row"] == 0 for row in _dup_target_de_findings), (
    "the duplicate-column warning for 'ru' must only appear under 'ru' itself, not leak into an unrelated "
    "language ('de') that has no duplicate of its own"
)

_dup_source_wb = openpyxl.Workbook()
_dup_source_ws = _dup_source_wb.active
_dup_source_ws.append(["Context", "ru", "en", "de", "ru"])
_dup_source_ws.append(["greeting", "Privet-B", "Hello", "Hallo", "Privet-E"])
_dup_source_buf = io.BytesIO()
_dup_source_wb.save(_dup_source_buf)
_dup_source_buf.seek(0)
_dup_source_sheets = _parse_workbook_direct(_dup_source_buf.read())
_dup_source_result = asyncio.run(_run_multi_check_direct(_dup_source_sheets, "ru", ["numbers"]))
_dup_source_all_warnings = [
    f
    for lang_findings in _dup_source_result["sheets"][0]["languages"].values()
    for row in lang_findings if row["excel_row"] == 0
    for f in row["findings"]
]
assert len(_dup_source_all_warnings) == 1, (
    "a duplicated SOURCE language must produce exactly ONE warning across the whole sheet (it affects every "
    "target language equally, so repeating it per language would misleadingly inflate the 'N found' count) "
    f"— got {len(_dup_source_all_warnings)}: {_dup_source_all_warnings}"
)
assert "СРАЗУ ВСЕХ" in _dup_source_all_warnings[0]["message"], (
    "a duplicated SOURCE language's warning must be worded distinctly from an ordinary duplicated target "
    "language, since it affects every checked language's report at once"
)
assert _dup_source_result["summary"]["total_findings"] == 1, (
    "the single source-duplicate warning must be counted once in the headline total, not once per language"
)

_clean_wb = openpyxl.Workbook()
_clean_ws = _clean_wb.active
_clean_ws.append(["Context", "en", "ru", "de"])
_clean_ws.append(["greeting", "Hello", "Privet", "Hallo"])
_clean_buf = io.BytesIO()
_clean_wb.save(_clean_buf)
_clean_buf.seek(0)
_clean_sheets = _parse_workbook_direct(_clean_buf.read())
assert _clean_sheets[0]["duplicate_language_columns"] == {}, (
    "a file with no duplicated language columns must report none"
)
_clean_result = asyncio.run(_run_multi_check_direct(_clean_sheets, "en", ["numbers"]))
assert not any(
    row["excel_row"] == 0
    for lang_findings in _clean_result["sheets"][0]["languages"].values()
    for row in lang_findings
), "a file with no duplicated columns must never show a duplicate-language warning"

# Same behavior on the large-file (Message Batch) path — build_batch_plan
# must carry duplicate_language_columns through its skeleton, and
# finalize_batch_results must apply the exact same warning logic once the
# (here: empty, no AI checks requested) batch "finishes".
_dup_batch_requests, _dup_batch_skeleton = _dup_build_batch_plan(_dup_target_sheets, "en", ["numbers"], "", None)
_dup_batch_result = _dup_finalize_batch_results(_dup_batch_skeleton, {})
_dup_batch_ru_findings = _dup_batch_result["sheets"][0]["languages"]["ru"]
assert any(
    row["excel_row"] == 0 and any("ru" in f["message"] for f in row["findings"])
    for row in _dup_batch_ru_findings
), "the batch (Message Batch / large-file) path must surface the same duplicate-language warning as the live path"

print("[OK] parse_workbook/run_multi_check/finalize_batch_results: a language code assigned to 2+ columns "
      "(e.g. two columns both headed \"ru\") is deduplicated in the languages list (so it's no longer "
      "checked, and billed, twice) and surfaced as a visible warning naming the columns involved — worded "
      "distinctly and shown exactly once when the duplicated column is the SOURCE language (since that "
      "affects every checked language at once), on both the live and the large-file batch check paths")

# Documenting a KNOWN, ACCEPTED gap found by subagent review: if a
# target_langs_filter ends up excluding every target language (nothing at
# all gets checked this run), a duplicated SOURCE language's warning has
# no language "slot" left in the RESULT to attach itself to, so it's
# silently absent from this particular response — even though nothing was
# actually checked or billed in that state either, and the separate
# pre-check /multi-check/detect-languages screen (see app.main) already
# surfaces this exact ambiguity before "start", regardless of which target
# languages the manager ends up selecting. Not worth restructuring the
# per-language result shape to cover a state where nothing is being
# checked at all — this test exists so the behavior stays a deliberate,
# understood choice rather than an unnoticed regression.
_dup_empty_result = asyncio.run(
    _run_multi_check_direct(_dup_source_sheets, "ru", ["numbers"], target_langs_filter=set())
)
assert _dup_empty_result["sheets"][0]["languages"] == {}, (
    "with every target language filtered out, nothing should be checked at all"
)
print("[OK] run_multi_check: documenting the accepted trade-off — when target_langs_filter excludes every "
      "target language, a duplicated source language's warning is absent from THIS result (nothing was "
      "checked or billed either), relying on the separate pre-check detect-languages screen to still "
      "surface the ambiguity before \"start\"")

# --- The actual real-world consequence of the duplicate-column bug, found
# on Александр's real file right after the warning above shipped: one of
# his two duplicate "ru" columns is COMPLETELY EMPTY (a stray leftover
# column), sitting to the right of the one with his real Russian source
# text. Before this fix, "rightmost wins" meant that empty column silently
# won on every row whenever "ru" was picked as the source language — this
# IS his very first "Источник пусто" ("source is empty") report from
# earlier in this project, which a merged-cells theory failed to explain
# because it was tested with "en" as source, never "ru". A blank duplicate
# must never win over a non-blank one; only a genuine disagreement between
# two REAL values still falls back to rightmost-wins (still flagged to the
# manager as a data-hygiene issue via the warning above, since THAT kind
# of collision genuinely needs a human decision, unlike an empty column). ---
_blank_dup_wb = openpyxl.Workbook()
_blank_dup_ws = _blank_dup_wb.active
_blank_dup_ws.append(["Context", "en", "ru", "de", "ru"])  # 2nd "ru" (col E) is the blank leftover
_blank_dup_ws.append(["greeting", "Hello", "Привет", "Hallo", None])
_blank_dup_buf = io.BytesIO()
_blank_dup_wb.save(_blank_dup_buf)
_blank_dup_buf.seek(0)
_blank_dup_sheets = _parse_workbook_direct(_blank_dup_buf.read())
assert _blank_dup_sheets[0]["rows"][0]["values"]["ru"] == "Привет", (
    "a blank duplicate column must NEVER win over a non-blank one for the same language code — this is "
    "the exact real bug behind Александр's original 'Источник пусто' report: an empty leftover 'ru' "
    "column silently produced an empty source on every row just because it happened to sit further right"
)
# Control: when duplicate columns genuinely DISAGREE (both non-blank, real
# but different values), rightmost-wins is still the fallback — unchanged
# from before this fix, and still surfaced via the warning above.
_conflict_dup_wb = openpyxl.Workbook()
_conflict_dup_ws = _conflict_dup_wb.active
_conflict_dup_ws.append(["Context", "en", "ru", "de", "ru"])
_conflict_dup_ws.append(["greeting", "Hello", "Привет-C", "Hallo", "Привет-E"])
_conflict_dup_buf = io.BytesIO()
_conflict_dup_wb.save(_conflict_dup_buf)
_conflict_dup_buf.seek(0)
_conflict_dup_sheets = _parse_workbook_direct(_conflict_dup_buf.read())
assert _conflict_dup_sheets[0]["rows"][0]["values"]["ru"] == "Привет-E", (
    "when both duplicate columns have real, DIFFERENT text, the rightmost one must still win, exactly as "
    "before this fix — only a blank duplicate should ever be skipped in favor of a non-blank one"
)
print("[OK] parse_workbook: a blank duplicate-language column never wins over a non-blank one with the "
      "same code (the real bug behind Александр's original 'Источник пусто' report — an empty leftover "
      "'ru' column silently beat the one with his real Russian text on every row), while two duplicate "
      "columns that genuinely disagree still resolve to the rightmost, exactly as before")

# Extra coverage recommended by subagent review of the fix above: 3+
# duplicate columns in various blank/real orders, and a whitespace-only
# cell (must arbitrate as blank — a real value elsewhere still wins — but
# must NOT be silently caught by the all-blank skip below it that a
# genuinely empty row relies on).
_triple_dup_wb = openpyxl.Workbook()
_triple_dup_ws = _triple_dup_wb.active
_triple_dup_ws.append(["Context", "en", "ru", "de", "ru", "ru"])
_triple_dup_ws.append(["blank-blank-real", "Hello", None, "Hallo", None, "Привет-real"])
_triple_dup_ws.append(["real-blank-real", "Hello", "Привет-C", "Hallo", None, "Привет-G"])
_triple_dup_ws.append(["blank-real-blank", "Hello", None, "Hallo", "Привет-E", None])
_triple_dup_buf = io.BytesIO()
_triple_dup_wb.save(_triple_dup_buf)
_triple_dup_buf.seek(0)
_triple_dup_sheets = _parse_workbook_direct(_triple_dup_buf.read())
_triple_dup_rows = {row["context"]: row for row in _triple_dup_sheets[0]["rows"]}
assert _triple_dup_rows["blank-blank-real"]["values"]["ru"] == "Привет-real", (
    "with 3 duplicate columns (blank, blank, real), the single real value must survive regardless of "
    "how many blanks come before it"
)
assert _triple_dup_rows["real-blank-real"]["values"]["ru"] == "Привет-G", (
    "with 3 duplicate columns (real, blank, real), the two real values genuinely disagree, so the "
    "rightmost one must win — a blank in the middle must not break that fallback"
)
assert _triple_dup_rows["blank-real-blank"]["values"]["ru"] == "Привет-E", (
    "with 3 duplicate columns (blank, real, blank), the single real value in the middle must survive "
    "despite a blank column after it"
)

_ws_dup_wb = openpyxl.Workbook()
_ws_dup_ws = _ws_dup_wb.active
_ws_dup_ws.append(["Context", "en", "ru", "de", "ru"])
_ws_dup_ws.append(["whitespace vs real", "Hello", "   ", "Hallo", "Привет"])
_ws_dup_ws.append(["all genuinely blank", None, "   ", None, "  "])
_ws_dup_buf = io.BytesIO()
_ws_dup_wb.save(_ws_dup_buf)
_ws_dup_buf.seek(0)
_ws_dup_sheets = _parse_workbook_direct(_ws_dup_buf.read())
_ws_dup_rows = {row["context"]: row for row in _ws_dup_sheets[0]["rows"]}
assert _ws_dup_rows["whitespace vs real"]["values"]["ru"] == "Привет", (
    "a whitespace-only duplicate column must be treated as blank for arbitration — a real value in "
    "another duplicate column of the same code must still win"
)
assert "all genuinely blank" not in _ws_dup_rows, (
    "a row where every language's value is blank or whitespace-only (including across duplicate columns) "
    "must still be skipped entirely, exactly as before this fix"
)
print("[OK] parse_workbook: 3+ duplicate columns resolve correctly in every blank/real order, and a "
      "whitespace-only duplicate column is treated as blank for arbitration without breaking the "
      "all-blank-row skip")

# --- run_ai_checks_batch: model_override bypasses _model_for_lang -------
# app.model_comparison (2026-09-22, Александр's "run a cheaper model
# several times" investigation) needs to force a SPECIFIC model regardless
# of what _model_for_lang would normally pick for the target language —
# this is the one piece of plumbing that makes that possible.
from app.claude_client import run_ai_checks_batch as _run_ai_checks_batch_direct

_override_seen_models = []


async def _fake_call_claude_records_model(prompt, model=None):
    _override_seen_models.append(model)
    return "[]", {"input_tokens": 5, "output_tokens": 2}, "end_turn"


settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
claude_client_mod._call_claude = _fake_call_claude_records_model
# "mr" is a hard language — _model_for_lang would normally pick
# CLAUDE_MODEL_HARD (Opus) here. model_override must win instead.
asyncio.run(_run_ai_checks_batch_direct(
    [{"context": "x", "source": "y", "translation": "z"}], ["typo"], target_lang="mr", source_lang="ru",
    model_override="claude-haiku-4-5-20251001",
))
claude_client_mod._call_claude = _previous_call_claude
settings.ANTHROPIC_API_KEY = ""
assert _override_seen_models == ["claude-haiku-4-5-20251001"], (
    f"model_override must be sent to _call_claude verbatim, overriding _model_for_lang's normal hard-language "
    f"pick (Opus) for 'mr' — got {_override_seen_models}"
)
print("[OK] run_ai_checks_batch: model_override forces a specific model regardless of what "
      "_model_for_lang would normally choose for the target language — every real production caller still "
      "leaves this unset and is unaffected")

# --- app.model_comparison: the standalone model-comparison diagnostic ---
# Built in direct response to Александр's "1 раз на sonnet и после 3 на
# haiku" idea (2026-09-22) — before changing anything about how hard
# languages actually get checked in production, this tool runs the same
# real row through Opus/Sonnet/Haiku several times each and reports a real
# hit rate per model, so that decision gets made from data, not more
# reasoning on paper. See app.model_comparison's own module comment.
from app.model_comparison import run_model_comparison as _run_model_comparison_direct
from app.model_comparison import MAX_RUNS_PER_MODEL as _MAX_RUNS_PER_MODEL

_cmp_counts = {"opus": 0, "sonnet": 0, "haiku": 0}


def _cmp_name_for(model_id):
    if model_id == settings.CLAUDE_MODEL_HARD:
        return "opus"
    if model_id == settings.CLAUDE_MODEL:
        return "sonnet"
    return "haiku"


async def _fake_call_claude_for_comparison(prompt, model=None):
    name = _cmp_name_for(model)
    _cmp_counts[name] += 1
    if name == "opus":
        # Opus catches it every single time — the whole reason it was
        # chosen for hard languages in the first place.
        return (
            '[{"row": 1, "type": "typo", "severity": "medium", "message": "opus поймала"}]',
            {"input_tokens": 40, "output_tokens": 20}, "end_turn",
        )
    if name == "sonnet":
        # Catches it on exactly 2 of however many calls it gets — an
        # aggregate count that holds regardless of which of the
        # concurrently-dispatched calls happens to complete first.
        if _cmp_counts[name] <= 2:
            return (
                '[{"row": 1, "type": "typo", "severity": "medium", "message": "sonnet поймала"}]',
                {"input_tokens": 40, "output_tokens": 20}, "end_turn",
            )
        return "[]", {"input_tokens": 40, "output_tokens": 2}, "end_turn"
    # Haiku never catches it at all in this test — modelling the "genuine
    # knowledge gap, not just bad luck" failure mode this whole comparison
    # exists to tell apart from a lucky/unlucky sample.
    return "[]", {"input_tokens": 40, "output_tokens": 2}, "end_turn"


settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
claude_client_mod._call_claude = _fake_call_claude_for_comparison
_cmp_report = asyncio.run(_run_model_comparison_direct(
    context="freebet", source="Фрибет без отыгрыша", translation="पैज न लावता फ्री बेट",
    target_lang="mr", source_lang="ru", checks=["typo"], runs_per_model=5,
    models=["opus", "sonnet", "haiku"],
))
claude_client_mod._call_claude = _previous_call_claude
settings.ANTHROPIC_API_KEY = ""

assert _cmp_counts == {"opus": 5, "sonnet": 5, "haiku": 5}, (
    f"each of the 3 candidate models must be called exactly runs_per_model times — got {_cmp_counts}"
)
assert _cmp_report["results"]["opus"]["catches"] == 5 and _cmp_report["results"]["opus"]["hit_rate"] == 1.0
assert _cmp_report["results"]["sonnet"]["catches"] == 2 and _cmp_report["results"]["sonnet"]["hit_rate"] == 0.4
assert _cmp_report["results"]["haiku"]["catches"] == 0 and _cmp_report["results"]["haiku"]["hit_rate"] == 0.0
assert _cmp_report["results"]["opus"]["example_messages"] == ["opus поймала"]
assert _cmp_report["results"]["haiku"]["example_messages"] == []
assert all(_cmp_report["results"][m]["cost_usd"] > 0 for m in ("opus", "sonnet", "haiku")), (
    "every model's cost_usd must reflect its own real calls, including the ones that found nothing "
    "(a $0 finding is still a real, billed API call)"
)
assert _cmp_report["total_cost_usd"] == round(sum(
    _cmp_report["results"][m]["cost_usd"] for m in ("opus", "sonnet", "haiku")
), 4), (
    "total_cost_usd must be exactly the sum of each candidate model's own (already-rounded) cost_usd, so "
    "adding up the per-model figures by hand always matches the printed total"
)
assert "Opus: поймала 5 из 5 прогонов (100%)" in _cmp_report["summary_ru"]
assert "Sonnet: поймала 2 из 5 прогонов (40%)" in _cmp_report["summary_ru"]
assert "Haiku: поймала 0 из 5 прогонов (0%)" in _cmp_report["summary_ru"]
print("[OK] run_model_comparison: runs the same real row through Opus/Sonnet/Haiku several times each and "
      "correctly tallies a per-model hit rate/cost/example findings from real (here, faked) per-model "
      "responses, including a ready-to-read Russian summary")

# --- run_model_comparison: raw_responses / silently-dropped-type warning ---
# Added 2026-09-23 during the Kyrgyz/French investigation: a model can
# genuinely respond with a finding, just under a "type" the run didn't ask
# for (e.g. it says "grammar" when only "typo" was requested) — that finding
# then gets silently removed by _filter_findings_by_checks, and from
# outside this looked EXACTLY like the model finding nothing at all. Every
# run's raw, unfiltered JSON now rides along in raw_responses so the two
# cases can finally be told apart.
_cmp_raw_counts = {"opus": 0}


async def _fake_call_claude_wrong_type(prompt, model=None):
    _cmp_raw_counts["opus"] += 1
    # The model DID notice something real — it just used a type name
    # ("grammar") outside the "typo"/"other" enum this run actually asked
    # for, so _filter_findings_by_checks must drop it from `findings`
    # while it still shows up untouched in the raw response.
    return (
        '[{"row": 1, "type": "grammar", "severity": "medium", "message": "модель сказала не то"}]',
        {"input_tokens": 40, "output_tokens": 20}, "end_turn",
    )


settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
claude_client_mod._call_claude = _fake_call_claude_wrong_type
_cmp_raw_report = asyncio.run(_run_model_comparison_direct(
    context="", source="исходник", translation="перевод",
    target_lang="ky", source_lang="ru", checks=["typo"], runs_per_model=2,
    models=["opus"],
))
claude_client_mod._call_claude = _previous_call_claude
settings.ANTHROPIC_API_KEY = ""

assert _cmp_raw_report["results"]["opus"]["catches"] == 0, (
    "a \"type\": \"grammar\" finding is outside the \"typo\"/\"other\" enum this run asked for, so it must "
    "never count as a catch — the whole point of this test is that this looks like a miss from `findings` alone"
)
_raw_entries = _cmp_raw_report["results"]["opus"]["raw_responses"]
assert len(_raw_entries) == 2, f"one raw entry per run (runs_per_model=2) — got {_raw_entries}"
assert all(entry == [{"row": 1, "type": "grammar", "severity": "medium", "message": "модель сказала не то"}]
           for entry in _raw_entries), (
    f"raw_responses must carry the model's response exactly as parsed, untouched by the type filter — got {_raw_entries}"
)
assert "⚠ модель что-то ответила, но это не прошло фильтр по типу" in _cmp_raw_report["summary_ru"], (
    "0 catches with non-empty raw_responses must be flagged in the Russian summary, not look like a silent, "
    "unremarkable miss"
)
print("[OK] run_model_comparison: raw_responses carries the model's unfiltered JSON even when "
      "_filter_findings_by_checks drops every finding for using a type outside this run's enum, and the "
      "Russian summary flags that specific situation instead of it looking like an ordinary 0/N miss")

# bare mode makes no schema/type promise to filter against, so there's
# nothing meaningful to put in raw_responses there — must stay an empty
# list per run rather than silently missing the key (a caller checking
# `report["results"][name]["raw_responses"]` must never KeyError just
# because this particular run happened to be bare).
settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
claude_client_mod._call_claude = _fake_call_claude_wrong_type
_cmp_raw_bare_report = asyncio.run(_run_model_comparison_direct(
    context="", source="исходник", translation="перевод",
    target_lang="ky", source_lang="ru", checks=["typo"], runs_per_model=1,
    models=["opus"], bare=True,
))
claude_client_mod._call_claude = _previous_call_claude
settings.ANTHROPIC_API_KEY = ""
assert _cmp_raw_bare_report["results"]["opus"]["raw_responses"] == [], (
    "bare mode has no type filter to look behind, so raw_responses must be present but empty, never missing"
)
print("[OK] run_model_comparison: bare mode always carries an (empty) raw_responses list, never a missing key")

# models= subset selection — Александр's ask, 2026-09-23: a routine
# comparison should default to Sonnet + Haiku only (no Opus spend), while
# an explicit models=[...] can still include Opus when actually needed.
# See app.model_comparison.DEFAULT_COMPARISON_MODELS.
from app.model_comparison import DEFAULT_COMPARISON_MODELS as _DEFAULT_COMPARISON_MODELS

assert _DEFAULT_COMPARISON_MODELS == ["sonnet", "haiku"], (
    f"the default comparison set must be exactly sonnet+haiku, no Opus — got {_DEFAULT_COMPARISON_MODELS}"
)

_models_counts = {"opus": 0, "sonnet": 0, "haiku": 0}


async def _fake_call_claude_records_model_name(prompt, model=None):
    _models_counts[_cmp_name_for(model)] += 1
    return "[]", {"input_tokens": 10, "output_tokens": 2}, "end_turn"


settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
claude_client_mod._call_claude = _fake_call_claude_records_model_name
_models_default_report = asyncio.run(_run_model_comparison_direct(
    context="freebet", source="Фрибет без отыгрыша", translation="पैज न लावता फ्री बेट",
    target_lang="mr", source_lang="ru", checks=["typo"], runs_per_model=1,
))
claude_client_mod._call_claude = _previous_call_claude
settings.ANTHROPIC_API_KEY = ""
assert _models_counts == {"opus": 0, "sonnet": 1, "haiku": 1}, (
    f"with no models= given, only sonnet and haiku must be called — Opus must NOT be called at all — "
    f"got {_models_counts}"
)
assert set(_models_default_report["results"].keys()) == {"sonnet", "haiku"}, (
    f"the report itself must only contain the models actually run — got {_models_default_report['results'].keys()}"
)
assert _models_default_report["models"] == ["sonnet", "haiku"]

# An unknown model name is silently dropped rather than raising or crashing.
_models_counts = {"opus": 0, "sonnet": 0, "haiku": 0}
settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
claude_client_mod._call_claude = _fake_call_claude_records_model_name
_models_filtered_report = asyncio.run(_run_model_comparison_direct(
    context="freebet", source="Фрибет без отыгрыша", translation="पैज न लावता फ्री बेट",
    target_lang="mr", source_lang="ru", checks=["typo"], runs_per_model=1,
    models=["haiku", "gpt5"],
))
claude_client_mod._call_claude = _previous_call_claude
settings.ANTHROPIC_API_KEY = ""
assert _models_counts == {"opus": 0, "sonnet": 0, "haiku": 1}, (
    f"an unknown model name must be silently dropped, leaving only the recognized ones — got {_models_counts}"
)
assert _models_filtered_report["models"] == ["haiku"]

# An empty/all-unknown models= list falls back to the default set rather
# than comparing against nothing.
_models_counts = {"opus": 0, "sonnet": 0, "haiku": 0}
settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
claude_client_mod._call_claude = _fake_call_claude_records_model_name
asyncio.run(_run_model_comparison_direct(
    context="freebet", source="Фрибет без отыгрыша", translation="पैज न लावता फ्री बेट",
    target_lang="mr", source_lang="ru", checks=["typo"], runs_per_model=1,
    models=["gpt5"],
))
claude_client_mod._call_claude = _previous_call_claude
settings.ANTHROPIC_API_KEY = ""
assert _models_counts == {"opus": 0, "sonnet": 1, "haiku": 1}, (
    f"an all-unknown models= list must fall back to the default sonnet+haiku set — got {_models_counts}"
)
print("[OK] run_model_comparison: models= defaults to sonnet+haiku only (no Opus spend on a routine "
      "comparison), an explicit list can still narrow further or include Opus, unknown model names are "
      "silently dropped, and an all-unknown list falls back to the default set")

# --- run_model_comparison: bare mode (minimal prompt, no calibration) ---
# Added 2026-09-22 after relaxed=True made ZERO difference to Sonnet's 0/5
# result — real evidence AGAINST the confidence-bar theory for Sonnet
# specifically. The next hypothesis: our normal prompt's sheer length/
# complexity (not the confidence sentence) is what costs Sonnet the
# nuance, since Александр got a correct answer from a bare, minimal
# question outside our pipeline. bare=True tests that in isolation.
from app.model_comparison import _parse_bare_response

# _parse_bare_response unit tests — the parsing logic itself, independent
# of any API call.
assert _parse_bare_response(None) == []
assert _parse_bare_response("") == []
assert _parse_bare_response("ПРОБЛЕМА: нет\nОБЪЯСНЕНИЕ:") == [], (
    "'ПРОБЛЕМА: нет' must never count as a catch"
)
assert _parse_bare_response("что-то совсем не по формату") == [], (
    "an unparseable response must count as 'no catch', not raise or false-positive"
)
_bare_parsed = _parse_bare_response(
    "ПРОБЛЕМА: да\nОБЪЯСНЕНИЕ: «без отыгрыша» переведено как «без ставки» — искажение смысла."
)
assert len(_bare_parsed) == 1 and _bare_parsed[0]["type"] == "typo", _bare_parsed
assert "искажение смысла" in _bare_parsed[0]["message"], _bare_parsed
# Exact match on "да", not a "starts with д" prefix check — subagent
# review (2026-09-22) caught that a prefix check would false-positive on
# any д-starting non-answer, silently inflating a model's apparent hit
# rate the moment it deviates from the requested да/нет format.
assert _parse_bare_response("ПРОБЛЕМА: действительно\nОБЪЯСНЕНИЕ: неважно") == [], (
    "'действительно' starts with 'д' but is NOT 'да' — must not be treated as a catch"
)
assert _parse_bare_response("ПРОБЛЕМА: дно\nОБЪЯСНЕНИЕ: неважно") == [], (
    "'дно' starts with 'д' but is NOT 'да' — must not be treated as a catch"
)
assert len(_parse_bare_response("ПРОБЛЕМА: Да!\nОБЪЯСНЕНИЕ: пример")) == 1, (
    "'Да!' (different case, trailing punctuation) must still be recognized as a real 'да'"
)
print("[OK] _parse_bare_response: 'ПРОБЛЕМА: да' with an explanation becomes exactly one finding, 'нет' "
      "and unparseable responses correctly count as no catch rather than raising or false-positiving")

# End-to-end: bare=True must bypass build_batch_prompt/calibration
# entirely (a genuinely different, minimal prompt with none of the normal
# machinery) and correctly tally catches from free-text "ПРОБЛЕМА: да/нет"
# answers instead of JSON.
_bare_seen_prompts = []


async def _fake_call_claude_bare(prompt, model=None):
    _bare_seen_prompts.append(prompt)
    name = _cmp_name_for(model)
    if name == "sonnet":
        # Modelling the real result Александр got outside our pipeline —
        # a bare question DOES get a correct answer from Sonnet.
        return (
            "ПРОБЛЕМА: да\nОБЪЯСНЕНИЕ: «без отыгрыша» — это условие по вейджеру, а «पैज न लावता» значит "
            "«не делая ставки» — другой смысл.",
            {"input_tokens": 20, "output_tokens": 15}, "end_turn",
        )
    return "ПРОБЛЕМА: нет\nОБЪЯСНЕНИЕ:", {"input_tokens": 20, "output_tokens": 5}, "end_turn"


settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
claude_client_mod._call_claude = _fake_call_claude_bare
_bare_report = asyncio.run(_run_model_comparison_direct(
    context="freebet", source="Фрибет без отыгрыша", translation="पैज न लावता फ्री बेट",
    target_lang="mr", source_lang="ru", checks=["typo"], runs_per_model=3, bare=True,
    models=["opus", "sonnet", "haiku"],
))
claude_client_mod._call_claude = _previous_call_claude
settings.ANTHROPIC_API_KEY = ""

assert _bare_report["bare"] is True
assert not any(
    ("Общее правило" in p) or ('"row"' in p) for p in _bare_seen_prompts
), (
    "bare mode's prompt must contain NONE of the normal pipeline's machinery (no calibration wording, no "
    "JSON row-numbered schema) — it must be the minimal BARE_COMPARISON_PROMPT only"
)
assert _bare_report["results"]["sonnet"]["catches"] == 3 and _bare_report["results"]["sonnet"]["hit_rate"] == 1.0, (
    f"a bare, minimal prompt must let the fake Sonnet catch it every time, mirroring the real result "
    f"Александр got outside our pipeline — got {_bare_report['results']['sonnet']}"
)
assert _bare_report["results"]["opus"]["catches"] == 0 and _bare_report["results"]["haiku"]["catches"] == 0
assert "🧪" in _bare_report["summary_ru"]
print("[OK] run_model_comparison: bare=True sends a genuinely minimal prompt (no calibration wording, no "
      "JSON schema) instead of the normal pipeline's, correctly parses free-text 'ПРОБЛЕМА: да/нет' answers")

# runs_per_model must be capped at MAX_RUNS_PER_MODEL — this hits the real,
# billed Anthropic API on every call, reachable without any of the usual
# project/manager plumbing, so an oversized request can't fire off an
# unbounded number of paid calls.
_cmp_counts2 = {"opus": 0, "sonnet": 0, "haiku": 0}


async def _fake_call_claude_counts_only(prompt, model=None):
    _cmp_counts2[_cmp_name_for(model)] += 1
    return "[]", {"input_tokens": 10, "output_tokens": 2}, "end_turn"


settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
claude_client_mod._call_claude = _fake_call_claude_counts_only
asyncio.run(_run_model_comparison_direct(
    context="a", source="b", translation="c", target_lang="mr", source_lang="ru",
    checks=["typo"], runs_per_model=999, models=["opus", "sonnet", "haiku"],
))
claude_client_mod._call_claude = _previous_call_claude
settings.ANTHROPIC_API_KEY = ""
assert _cmp_counts2 == {"opus": _MAX_RUNS_PER_MODEL, "sonnet": _MAX_RUNS_PER_MODEL, "haiku": _MAX_RUNS_PER_MODEL}, (
    f"runs_per_model=999 must be silently capped at MAX_RUNS_PER_MODEL ({_MAX_RUNS_PER_MODEL}) per model, not "
    f"actually fire 999 real paid calls per model — got {_cmp_counts2}"
)
print(f"[OK] run_model_comparison: an oversized runs_per_model is capped at MAX_RUNS_PER_MODEL "
      f"({_MAX_RUNS_PER_MODEL}) per model instead of firing an unbounded number of real paid API calls")

# A register_value entry (the register-reporting side channel, not a real
# problem) must never count as a "catch" — a model whose only response is
# "here's the register" caught NOTHING for the purposes of this test.
_cmp_calls_register = {"n": 0}


async def _fake_call_claude_register_only(prompt, model=None):
    _cmp_calls_register["n"] += 1
    return (
        '[{"row": 1, "type": "register_value", "value": "formal"}]',
        {"input_tokens": 10, "output_tokens": 5}, "end_turn",
    )


settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
claude_client_mod._call_claude = _fake_call_claude_register_only
_cmp_register_report = asyncio.run(_run_model_comparison_direct(
    context="freebet", source="Фрибет без отыгрыша", translation="पैज न लावता फ्री बेट",
    target_lang="mr", source_lang="ru", checks=["typo", "register"], runs_per_model=2,
    models=["opus", "sonnet", "haiku"],
))
claude_client_mod._call_claude = _previous_call_claude
settings.ANTHROPIC_API_KEY = ""
assert all(_cmp_register_report["results"][m]["catches"] == 0 for m in ("opus", "sonnet", "haiku")), (
    f"a register_value-only response must never count as a catch — got {_cmp_register_report['results']}"
)
print("[OK] run_model_comparison: a register_value entry (the register side-channel, not a real problem) "
      "is correctly excluded from counting as a 'catch'")

# No ANTHROPIC_API_KEY configured -> graceful no-op (same pattern as the
# rest of the AI-check pipeline), not an error and not a real call.
_cmp_counts3 = {"n": 0}


async def _fake_call_claude_should_not_be_called(prompt, model=None):
    _cmp_counts3["n"] += 1
    return "[]", {}, "end_turn"


claude_client_mod._call_claude = _fake_call_claude_should_not_be_called
assert settings.ANTHROPIC_API_KEY == ""
_cmp_empty = asyncio.run(_run_model_comparison_direct(
    context="a", source="b", translation="c", target_lang="mr", source_lang="ru",
))
claude_client_mod._call_claude = _previous_call_claude
assert _cmp_empty == {}, "with no ANTHROPIC_API_KEY configured, run_model_comparison must return {} without calling anything"
assert _cmp_counts3["n"] == 0, "no ANTHROPIC_API_KEY configured must mean zero real calls, not calls that get discarded"
print("[OK] run_model_comparison: gracefully returns {} and makes zero calls when no ANTHROPIC_API_KEY is "
      "configured, same graceful no-op as the rest of the AI-check pipeline")

# --- /debug/model-comparison endpoint ------------------------------------
settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
claude_client_mod._call_claude = _fake_call_claude_for_comparison
_cmp_counts["opus"] = _cmp_counts["sonnet"] = _cmp_counts["haiku"] = 0
r = check("POST /debug/model-comparison (defaults = the real Marathi row, models omitted)", client.post(
    "/debug/model-comparison", json={"runs_per_model": 3},
))
claude_client_mod._call_claude = _previous_call_claude
settings.ANTHROPIC_API_KEY = ""
_ep_body = r.json()
assert _ep_body["target_lang"] == "mr", _ep_body
# ModelComparisonIn.models defaults to ["sonnet", "haiku"] (Александр's
# ask, 2026-09-23 — no Opus spend on a routine comparison) — so leaving
# "models" out of the request body must NOT pull Opus in.
assert set(_ep_body["results"].keys()) == {"sonnet", "haiku"}, _ep_body
assert "summary_ru" in _ep_body and "Opus" not in _ep_body["summary_ru"], _ep_body
assert _cmp_counts["opus"] == 0, "Opus must not be called at all when the request omits \"models\""

settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
claude_client_mod._call_claude = _fake_call_claude_for_comparison
_cmp_counts["opus"] = _cmp_counts["sonnet"] = _cmp_counts["haiku"] = 0
r = check("POST /debug/model-comparison with models=[opus, sonnet, haiku] explicitly", client.post(
    "/debug/model-comparison", json={"runs_per_model": 3, "models": ["opus", "sonnet", "haiku"]},
))
claude_client_mod._call_claude = _previous_call_claude
settings.ANTHROPIC_API_KEY = ""
_ep_body_all = r.json()
assert set(_ep_body_all["results"].keys()) == {"opus", "sonnet", "haiku"}, _ep_body_all
assert "Opus" in _ep_body_all["summary_ru"], _ep_body_all

r = check(
    "POST /debug/model-comparison without ANTHROPIC_API_KEY -> 503, not a silent empty success",
    client.post("/debug/model-comparison", json={}), expect=503,
)
print("[OK] POST /debug/model-comparison: the diagnostic endpoint returns a real per-model comparison "
      "(defaulting to the actual Marathi 'отыгрыш' row that started this investigation) when an API key is "
      "configured, defaults to sonnet+haiku only (no Opus spend) when \"models\" is omitted, still supports "
      "an explicit Opus request, and returns a clear 503 instead of a silently empty/misleading success "
      "when no API key is configured")

settings.ANTHROPIC_API_KEY = "fake-key-for-smoketest"
claude_client_mod._call_claude = _fake_call_claude_bare
r = check("POST /debug/model-comparison with bare=true", client.post(
    "/debug/model-comparison", json={"runs_per_model": 2, "bare": True},
))
claude_client_mod._call_claude = _previous_call_claude
settings.ANTHROPIC_API_KEY = ""
_ep_bare_body = r.json()
assert _ep_bare_body["bare"] is True, _ep_bare_body
assert _ep_bare_body["results"]["sonnet"]["catches"] == 2, _ep_bare_body
print("[OK] POST /debug/model-comparison: the bare=true field reaches run_model_comparison end-to-end "
      "through the request schema, not just when called directly in Python")

print("\nALL SMOKETEST CHECKS PASSED")
