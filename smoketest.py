"""Quick local smoke test for the shared-folders / admin model, the
structured glossary upload, and the new check types. Uses a throwaway
SQLite DB (no DATABASE_URL set) and no AI key, so only rule-based findings
are expected, not AI ones."""
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

# --- listing folders (public, no code) ---
r = check("list managers", client.get("/managers"))
assert len(r.json()) == 2

# --- unlock flow ---
check("unlock wrong code", client.post(f"/managers/{regular_id}/unlock", json={"code": "wrong"}), expect=401)
check("unlock correct code", client.post(f"/managers/{regular_id}/unlock", json={"code": "5678"}))

# --- non-admin blocked from structural changes ---
check("non-admin create project blocked", client.post("/projects", json={"name": "Pragmatic Play Promo", "manager_id": regular_id}), expect=403)

# --- admin creates project ---
r = check("admin create project", client.post("/projects", json={"name": "Pragmatic Play Promo", "manager_id": admin_id}))
project_id = r.json()["id"]
assert r.json()["created_by_name"] == "Александр"
assert r.json()["glossary_filename"] == ""

# --- new project auto-gets the standard set of language folders ---
r = check("auto-created language folders", client.get(f"/projects/{project_id}/languages"))
auto_langs = {l["lang_code"] for l in r.json()}
expected = {
    "en", "ar", "az", "bd", "de", "el", "es", "es-mx", "es-ar", "fr-ci",
    "hi", "hing", "id", "it", "jp", "kz", "ko", "kg", "mr", "ms", "pl",
    "pt-br", "ro", "ru", "sw", "te", "tj", "th", "tl", "tr", "ua", "ur",
    "uz", "vi", "cn",
}
assert auto_langs == expected, f"missing: {expected - auto_langs}, extra: {auto_langs - expected}"
print(f"   {len(auto_langs)} language folders auto-created")

check("duplicate project name", client.post("/projects", json={"name": "Pragmatic Play Promo", "manager_id": admin_id}), expect=409)

r = check("ru language id lookup", client.get(f"/projects/{project_id}/languages"))
language_id = next(l["id"] for l in r.json() if l["lang_code"] == "ru")

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

# --- non-admin blocked from adding language ---
check("non-admin add language blocked", client.post(
    f"/projects/{project_id}/languages", json={"lang_code": "xx-test", "manager_id": regular_id}
), expect=403)

# --- BOTH folders see the same shared project/language (no manager scoping) ---
r = check("list projects (shared)", client.get("/projects"))
assert len(r.json()) == 1
r = check("list languages (shared)", client.get(f"/projects/{project_id}/languages"))
assert len(r.json()) == 35

# --- non-admin CAN run a single check, glossary narrowed to EN+RU+target ---
r = check("non-admin single check (ru)", client.post("/check", json={
    "source": "The bonus is $50 and expires in 3 days.",
    "translation": "Бонус составляет $500 и истекает через 3 дня",
    "checks": ["numbers", "placeholders", "glossary", "register", "typo", "untranslatable", "completeness", "punctuation"],
    "project_id": project_id,
    "language_id": language_id,
    "manager_name": "Мария",
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
    "language_id": language_id,
    "extra_instructions": "В этой задаче 'Golden Spin' нужно переводить как 'Голден Спин'.",
    "manager_name": "Мария",
}))

# --- history shows who performed it ---
r = check("history shows attribution", client.get(f"/projects/{project_id}/languages/{language_id}/history"))
assert len(r.json()) == 2  # the ru single check + the extra_instructions one; the double-space check was standalone
assert r.json()[0]["performed_by_name"] == "Мария"
print("   performed_by_name:", r.json()[0]["performed_by_name"])

# --- non-admin CAN run a multi-check upload ---
sample_path = "/root/.claude/uploads/aee9e6e5-e96f-5b4b-aa4e-8aad6284c8c9/147efc1b-Promo_Rules_Localization.xlsx"
with open(sample_path, "rb") as f:
    r = check("non-admin multi-check upload", client.post(
        f"/projects/{project_id}/multi-check",
        files={"file": ("Promo_Rules_Localization.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"source_lang": "", "manager_name": "Мария", "extra_instructions": ""},
    ))
multi_data = r.json()
multi_check_id = multi_data["multi_check_id"]
print("   summary:", multi_data["summary"])

r = check("multi-check history shows attribution", client.get(f"/projects/{project_id}/multi-check"))
assert r.json()[0]["performed_by_name"] == "Мария"

check("multi-check report download", client.get(
    f"/projects/{project_id}/multi-check/{multi_check_id}/report.xlsx"
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

# --- re-running migrations on an already-migrated DB should be a no-op ---
dbmod.init_db()
r = check("managers survive re-migration", client.get("/managers"))
assert len(r.json()) == 2
r = check("projects survive re-migration", client.get("/projects"))
assert len(r.json()) == 1
r = check("glossary survives re-migration", client.get(f"/projects/{project_id}/glossary/status"))
assert r.json()["term_count"] == 1

print("\nALL SMOKETEST CHECKS PASSED")
