"""Quick local smoke test for the shared-folders / admin model, the
structured tone-of-address document, and the new check types. Uses
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
assert r.json()["tone_filename"] == ""

check("duplicate project name", client.post("/projects", json={"name": "Pragmatic Play Promo", "manager_id": admin_id}), expect=409)

# --- no more language folders: everything runs directly against the project ---
import openpyxl

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
# export — language codes as header-row columns, not one row per language
# as originally assumed, with the register in the data row below each
# column. "KZ" is the same country-code-style label the real file uses
# for Kazakh.
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

# --- known languages come from the tone-of-address document alone now
# (the only reference document left) — just every language column it names ---
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

# --- detect-languages: the target-language checkbox list must come from
# the UPLOADED FILE's own columns, not only from the project's Tone
# document — Александр hit a real gap where English simply had no row in
# his Tone document (it rarely needs a ты/вы-style rule) and so never
# appeared as a selectable target at all, no matter what the file
# contained. This endpoint is what lets the frontend show the file's own
# languages instead. ---
with open(sample_path, "rb") as f:
    r = check("detect-languages reports the file's own language columns", client.post(
        f"/projects/{project_id}/multi-check/detect-languages",
        files={"file": ("Promo_Rules_Localization.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    ))
detected = r.json()["languages"]
assert "ru" in detected, detected
assert len(detected) > 2, detected  # this sample file spans many target languages
print(f"   detected languages: {detected}")

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


async def _fake_create_message_batch(requests):
    assert requests, "expected at least one per-language batch request to be built"
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
    # One fake AI finding for the "ru" target language of the (only) sheet —
    # custom_id format is "s{sheet_index}-{lang}" (see build_batch_plan).
    # Shape matches the real get_batch_results: {custom_id: {"text": ...,
    # "usage": ..., "stop_reason": ..., "result_type": ...}}.
    return {
        "s0-ru": {
            "text": '[{"row": 1, "type": "typo", "severity": "medium", "message": "тестовая ИИ-находка"}]',
            "usage": {"input_tokens": 1000, "output_tokens": 200},
            "stop_reason": "end_turn",
            "result_type": "succeeded",
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

# --- re-uploading the tone doc replaces it, doesn't accumulate ---
twb3 = openpyxl.Workbook()
tws3 = twb3.active
tws3.append(["EN", "RU"])
tws3.append(["Формальное", "Формальное"])
tbuf3 = io.BytesIO()
twb3.save(tbuf3)
tbuf3.seek(0)
r = check("admin re-uploads tone doc (replaces)", client.post(
    f"/projects/{project_id}/tone/upload",
    files={"file": ("tone2.xlsx", tbuf3, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    data={"manager_id": admin_id},
))
assert r.json()["rule_count"] == 2, r.json()

# --- project template copy: new project starts with the same docs, fully
# independent afterward (editing one never touches the other) ---
r = check("create project copying requirements from the first", client.post(
    "/projects", json={"name": "LS Promo", "manager_id": admin_id, "copy_from_project_id": project_id}
))
copy_project_id = r.json()["id"]
assert r.json()["tone_filename"] == "tone2.xlsx", r.json()

r = check("copied project's tone status matches source", client.get(f"/projects/{copy_project_id}/tone/status"))
assert r.json()["rule_count"] == 2, r.json()

# re-uploading the ORIGINAL project's tone doc must not affect the copy
twb4 = openpyxl.Workbook()
tws4 = twb4.active
tws4.append(["EN", "RU", "ES (MX)"])
tws4.append(["Формальное", "Формальное", "Неформальное"])
tbuf4 = io.BytesIO()
twb4.save(tbuf4)
tbuf4.seek(0)
check("re-upload original project's tone doc again", client.post(
    f"/projects/{project_id}/tone/upload",
    files={"file": ("tone3.xlsx", tbuf4, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    data={"manager_id": admin_id},
))
r = check("copy's tone doc is untouched by the original's re-upload", client.get(f"/projects/{copy_project_id}/tone/status"))
assert r.json()["rule_count"] == 2, r.json()

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
r = check("tone doc survives re-migration", client.get(f"/projects/{project_id}/tone/status"))
assert r.json()["rule_count"] == 3

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

# parse_tone_workbook: real layout is column-per-language, not
# row-per-language as originally assumed — including a country-code
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

# --- the prompt also tells the model not to squeeze an out-of-scope
# finding into whichever type happens to be the only one allowed —
# Александр hit exactly this with the (now-removed) glossary check, but
# the instruction itself is generic, not glossary-specific, so it stays
# relevant for any single-check-type run. ---
from app.claude_client import SINGLE_PROMPT, BATCH_PROMPT

assert "Не подгоняй" in SINGLE_PROMPT and "Не подгоняй" in BATCH_PROMPT
print("[OK] the prompt explicitly forbids squeezing an out-of-scope finding into whichever "
      "type happens to be the only one allowed")

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
type_enum_line = next(line for line in captured_prompts[0].splitlines() if '"type":' in line)
assert "typo" in type_enum_line and "untranslatable" not in type_enum_line, type_enum_line
print("[OK] AI findings hard-filtered to requested checks even when the model reports "
      "an out-of-scope finding anyway (prompt's type list is also scoped down, in addition)")

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
                "custom_id": "s0-es-mx", "model": "claude-haiku-4-5-20251001",
                "number_to_index": {}, "rows": [{"excel_row": 2, "context": "", "source": "a", "translation": "b", "findings": []}],
            },
            "fr": {
                "custom_id": "s0-fr", "model": "claude-haiku-4-5-20251001",
                "number_to_index": {}, "rows": [{"excel_row": 2, "context": "", "source": "a", "translation": "b", "findings": []}],
            },
            "de": {
                # never made it into the results at all (e.g. dropped between submit and poll)
                "custom_id": "s0-de", "model": "claude-haiku-4-5-20251001",
                "number_to_index": {}, "rows": [{"excel_row": 2, "context": "", "source": "a", "translation": "b", "findings": []}],
            },
        },
    }],
}
fake_batch_results = {
    "s0-es-mx": {"text": "[", "usage": {"input_tokens": 10, "output_tokens": 8000}, "stop_reason": "max_tokens", "result_type": "succeeded"},
    "s0-fr": {"text": None, "usage": {}, "stop_reason": None, "result_type": "errored"},
    # "s0-de" deliberately absent
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

print("\nALL SMOKETEST CHECKS PASSED")
