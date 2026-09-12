"""Quick local smoke test for the shared-folders / admin model, the
structured glossary/tone documents, and the new check types. Uses
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
assert r.json()["glossary_filename"] == ""
assert r.json()["tone_filename"] == ""

check("duplicate project name", client.post("/projects", json={"name": "Pragmatic Play Promo", "manager_id": admin_id}), expect=409)

# --- no more language folders: everything runs directly against the project ---

# --- build a glossary workbook matching the agency's real doc shape:
# EN | Пояснение | RU | ES (MX) | ...
import openpyxl
gwb = openpyxl.Workbook()
gws = gwb.active
gws.append(["EN", "Пояснение", "RU", "ES (MX)"])
gws.append(["Mission Rush", "название турнира, не переводить", "Mission Rush", "Mission Rush"])
gws.append(["Golden Spin", "название бонуса", "Голден Спин", "Golden Spin"])
gws.append(["$5,000", "формат валюты для примера", "5 000$", "$5,000"])
gbuf = io.BytesIO()
gwb.save(gbuf)
gbuf.seek(0)

check("non-admin glossary upload blocked", client.post(
    f"/projects/{project_id}/glossary/upload",
    files={"file": ("glossary.xlsx", gbuf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    data={"manager_id": regular_id},
), expect=403)

gbuf.seek(0)
r = check("admin glossary upload", client.post(
    f"/projects/{project_id}/glossary/upload",
    files={"file": ("glossary.xlsx", gbuf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    data={"manager_id": admin_id},
))
assert r.json()["term_count"] == 3, r.json()
assert r.json()["filename"] == "glossary.xlsx"

r = check("glossary status (visible to any folder)", client.get(f"/projects/{project_id}/glossary/status"))
assert r.json()["term_count"] == 3

# --- doc-gating: tone (register) check refuses to run until its doc exists ---
check("tone (register) check blocked with no doc uploaded", client.post("/check", json={
    "source": "Play now.",
    "translation": "Играйте сейчас.",
    "checks": ["register"],
    "project_id": project_id,
    "source_lang": "en",
    "target_lang": "ru",
    "manager_name": "Мария",
    "manager_id": regular_id,
}), expect=400)

# --- Tone-of-address doc: real layout, matching the agency's actual
# export — mirrors the Glossary (language codes as header-row columns,
# not one row per language as originally assumed), with the register in
# the data row below each column. "KZ" is the same country-code-style
# label the real file uses for Kazakh.
twb = openpyxl.Workbook()
tws = twb.active
tws.append(["EN", "RU", "KZ", "ES (MX)"])
tws.append(["Формальное", "Формальное", "Формальное", "Неформальное"])
tbuf = io.BytesIO()
twb.save(tbuf)
tbuf.seek(0)
r = check("admin tone upload", client.post(
    f"/projects/{project_id}/tone/upload",
    files={"file": ("tone.xlsx", tbuf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    data={"manager_id": admin_id},
))
assert r.json()["rule_count"] == 4, r.json()

# --- known-languages union across the two remaining docs (glossary, tone):
# just the union of every language column named in either, with no
# granularity-bridging needed here since both docs already use the same
# plain codes ---
r = check("known languages union", client.get(f"/projects/{project_id}/known-languages"))
assert set(r.json()["languages"]) == {"ru", "es-mx", "kz", "en"}, r.json()

# --- now that the tone doc exists, the previously-blocked check runs fine ---
check("tone check now runs", client.post("/check", json={
    "source": "Play now.",
    "translation": "Играйте сейчас.",
    "checks": ["register"],
    "project_id": project_id,
    "source_lang": "en",
    "target_lang": "ru",
    "manager_name": "Мария",
    "manager_id": regular_id,
}))

# --- BOTH folders see the same shared project (no manager scoping) ---
r = check("list projects (shared)", client.get("/projects"))
assert len(r.json()) == 1

# --- non-admin CAN run a single check, glossary narrowed to EN+RU+target ---
r = check("non-admin single check (ru)", client.post("/check", json={
    "source": "The bonus is $50 and expires in 3 days.",
    "translation": "Бонус составляет $500 и истекает через 3 дня",
    "checks": ["numbers", "placeholders", "glossary", "register", "typo", "untranslatable", "completeness", "punctuation"],
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
assert len(history) == 3  # tone + full-checks + extra_instructions check (double-space was standalone)
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

check("multi-check report download", client.get(
    f"/projects/{project_id}/multi-check/{multi_check_id}/report.xlsx", params={"manager_id": regular_id}
))

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

# --- large multi-check actually goes through the Message Batches path when
# a batch can be submitted — simulate that here (no real Anthropic key in
# this sandbox) by faking the three network calls, to prove the
# submit -> poll -> merge-AI-findings-into-the-rule-based-skeleton pipeline
# actually produces a correct, complete result. ---
import app.excel_multi as excel_multi_mod


async def _fake_create_message_batch(requests):
    assert requests, "expected at least one per-language batch request to be built"
    return "msgbatch_test123"


async def _fake_get_batch_status(batch_id):
    assert batch_id == "msgbatch_test123"
    return {"processing_status": "ended", "results_url": "fake://results"}


async def _fake_get_batch_results(results_url):
    assert results_url == "fake://results"
    # One fake AI finding for the "ru" target language of the (only) sheet —
    # custom_id format is "s{sheet_index}-{lang}" (see build_batch_plan).
    # Shape matches the real get_batch_results: {custom_id: {"text": ..., "usage": ...}}.
    return {
        "s0-ru": {
            "text": '[{"row": 1, "type": "typo", "severity": "medium", "message": "тестовая ИИ-находка"}]',
            "usage": {"input_tokens": 1000, "output_tokens": 200},
        }
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

r = check("history shows the batch entry as processing", client.get(
    f"/projects/{project_id}/multi-check", params={"manager_id": regular_id}
))
assert r.json()[0]["status"] == "processing", r.json()[0]

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
r2 = check("multi-check detail re-fetch still shows the persisted cost", client.get(
    f"/projects/{project_id}/multi-check/{batch_multi_check_id}", params={"manager_id": regular_id}
))
assert r2.json()["cost_usd"] == finalized["cost_usd"], r2.json()
print(f"   batch cost_usd: {finalized['cost_usd']}")

r = check("history now shows completed", client.get(
    f"/projects/{project_id}/multi-check", params={"manager_id": regular_id}
))
assert r.json()[0]["status"] == "completed"

check("report download works once completed", client.get(
    f"/projects/{project_id}/multi-check/{batch_multi_check_id}/report.xlsx", params={"manager_id": regular_id}
))

# --- re-uploading the glossary replaces it, doesn't accumulate ---
gwb2 = openpyxl.Workbook()
gws2 = gwb2.active
gws2.append(["EN", "Пояснение", "RU"])
gws2.append(["Free Spins", "бонусные вращения", "Фриспины"])
gbuf2 = io.BytesIO()
gwb2.save(gbuf2)
gbuf2.seek(0)
r = check("admin re-uploads glossary (replaces)", client.post(
    f"/projects/{project_id}/glossary/upload",
    files={"file": ("glossary2.xlsx", gbuf2, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    data={"manager_id": admin_id},
))
assert r.json()["term_count"] == 1, r.json()

# --- project template copy: new project starts with the same docs, fully
# independent afterward (editing one never touches the other) ---
r = check("create project copying requirements from the first", client.post(
    "/projects", json={"name": "LS Promo", "manager_id": admin_id, "copy_from_project_id": project_id}
))
copy_project_id = r.json()["id"]
assert r.json()["glossary_filename"] == "glossary2.xlsx", r.json()

r = check("copied project's glossary status matches source", client.get(f"/projects/{copy_project_id}/glossary/status"))
assert r.json()["term_count"] == 1, r.json()

# re-uploading the ORIGINAL project's glossary must not affect the copy
gwb3 = openpyxl.Workbook()
gws3 = gwb3.active
gws3.append(["EN", "RU"])
gws3.append(["Term A", "Термин А"])
gws3.append(["Term B", "Термин Б"])
gbuf3 = io.BytesIO()
gwb3.save(gbuf3)
gbuf3.seek(0)
check("re-upload original project's glossary again", client.post(
    f"/projects/{project_id}/glossary/upload",
    files={"file": ("glossary3.xlsx", gbuf3, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    data={"manager_id": admin_id},
))
r = check("copy's glossary is untouched by the original's re-upload", client.get(f"/projects/{copy_project_id}/glossary/status"))
assert r.json()["term_count"] == 1, r.json()

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
r = check("glossary survives re-migration", client.get(f"/projects/{project_id}/glossary/status"))
assert r.json()["term_count"] == 2

# --- unit tests: language-code granularity bridging (resolve_lang_code /
# merge_lang_codes) ---
from app.excel_multi import resolve_lang_code, merge_lang_codes
from app.project_docs import parse_tone_workbook

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

# parse_tone_workbook: real layout is column-per-language (Glossary-style),
# not row-per-language as originally assumed — including a country-code
# label ("KZ") and a combined "/"-separated header applying to two codes
twb2 = openpyxl.Workbook()
tws2 = twb2.active
tws2.append(["EN", "RU", "KZ", "FR-CI / FR-FR", "ES (MX)"])
tws2.append(["Формальное", "Формальное", "Формальное", "Неформальное", "Неформальное"])
tbuf2 = io.BytesIO()
twb2.save(tbuf2)
tbuf2.seek(0)
tone_rows = {r["lang_code"]: r["register"] for r in parse_tone_workbook(tbuf2.read())}
assert tone_rows == {
    "en": "formal", "ru": "formal", "kz": "formal",
    "fr-ci": "informal", "fr-fr": "informal", "es-mx": "informal",
}, tone_rows
print("[OK] parse_tone_workbook: column-per-language layout (not row-per-language), "
      "combined header split across both codes")

# --- model tiering: confirmed "hard" languages get the stronger model,
# matched by base subtag so any region variant of them qualifies too ---
from app.claude_client import _model_for_lang
from app.config import settings

for hard in ["kk", "kk-KZ", "ky-KG", "tg-TJ", "uz", "sw-KE", "te-IN", "mr-IN", "az-AZ"]:
    assert _model_for_lang(hard) == settings.CLAUDE_MODEL_HARD, hard
for normal in ["ru", "es-mx", "en", "de-DE", "fr"]:
    assert _model_for_lang(normal) == settings.CLAUDE_MODEL, normal
print("[OK] _model_for_lang: confirmed hard-language list (kk/ky/tg/uz/sw/te/mr/az) "
      "routes to CLAUDE_MODEL_HARD by base subtag, everything else to CLAUDE_MODEL")

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

# --- the "typo" AI check catches a WRONG CURRENCY entirely (e.g. euro
# instead of dollar) as a genuine translation error, not just a stylistic
# quirk — Александр hit a real case where $0.40 was mistranslated as
# "0,40 €" ---
from app.claude_client import CHECK_LABELS

assert "ДРУГАЯ ВАЛЮТА" in CHECK_LABELS["typo"], CHECK_LABELS["typo"]
print("[OK] currency identity (wrong currency, e.g. € instead of $) is scoped to «опечатки/ошибки»")

# --- AI findings are hard-filtered to only the checks actually requested,
# even if the model ignores the prompt's instruction and reports something
# else anyway ---
from app.claude_client import _allowed_ai_types, _filter_findings_by_checks, run_ai_checks
import app.claude_client as claude_client_mod

assert _allowed_ai_types(["typo"]) == {"typo"}
assert _allowed_ai_types(["typo", "punctuation", "max_length"]) == {"typo"}  # rule checks aren't AI types

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
    return (text, {"input_tokens": 500, "output_tokens": 100})


claude_client_mod._call_claude = _fake_call_claude
findings, ai_cost = asyncio.get_event_loop().run_until_complete(
    run_ai_checks(
        "source", "translation", "", ["typo"], target_lang="az-az",
    )
)
assert findings == [raw_findings[0]], findings
assert ai_cost > 0, ai_cost
# The JSON schema shown to the model is also scoped down to just the
# requested check(s), not a fixed always-all list.
type_enum_line = next(line for line in captured_prompts[0].splitlines() if '"type":' in line)
assert "typo" in type_enum_line and "untranslatable" not in type_enum_line, type_enum_line
print("[OK] AI findings hard-filtered to requested checks even when the model reports "
      "an out-of-scope finding anyway (prompt's type list is also scoped down, in addition)")

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

print("\nALL SMOKETEST CHECKS PASSED")
