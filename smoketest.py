"""Quick local smoke test for the shared-folders / admin model.
Uses a throwaway SQLite DB (no DATABASE_URL set) and no AI key, so only
rule-based findings are expected, not AI ones."""
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

check("duplicate project name", client.post("/projects", json={"name": "Pragmatic Play Promo", "manager_id": admin_id}), expect=409)

# --- non-admin blocked from glossary edit / adding language ---
check("non-admin edit glossary blocked", client.put(
    f"/projects/{project_id}/glossary", json={"glossary": "x", "manager_id": regular_id}
), expect=403)
check("non-admin add language blocked", client.post(
    f"/projects/{project_id}/languages", json={"lang_code": "ru", "manager_id": regular_id}
), expect=403)

# --- admin sets glossary + adds language ---
check("admin set glossary", client.put(
    f"/projects/{project_id}/glossary", json={"glossary": "Mission Rush -> Mission Rush", "manager_id": admin_id}
))
r = check("admin add language", client.post(
    f"/projects/{project_id}/languages", json={"lang_code": "ru", "manager_id": admin_id}
))
language_id = r.json()["id"]

# --- BOTH folders see the same shared project/language (no manager scoping) ---
r = check("list projects (shared)", client.get("/projects"))
assert len(r.json()) == 1
r = check("list languages (shared)", client.get(f"/projects/{project_id}/languages"))
assert len(r.json()) == 1

# --- non-admin CAN run a single check ---
r = check("non-admin single check", client.post("/check", json={
    "source": "The bonus is $50 and expires in 3 days.",
    "translation": "Бонус составляет $500 и истекает через 3 дня.",
    "checks": ["numbers", "placeholders", "glossary", "register", "typo"],
    "project_id": project_id,
    "language_id": language_id,
    "manager_name": "Мария",
}))
print("   findings:", r.json()["findings"])

# --- history shows who performed it ---
r = check("history shows attribution", client.get(f"/projects/{project_id}/languages/{language_id}/history"))
assert len(r.json()) == 1
assert r.json()[0]["performed_by_name"] == "Мария"
print("   performed_by_name:", r.json()[0]["performed_by_name"])

# --- non-admin CAN run a multi-check upload ---
sample_path = "/root/.claude/uploads/aee9e6e5-e96f-5b4b-aa4e-8aad6284c8c9/147efc1b-Promo_Rules_Localization.xlsx"
with open(sample_path, "rb") as f:
    r = check("non-admin multi-check upload", client.post(
        f"/projects/{project_id}/multi-check",
        files={"file": ("Promo_Rules_Localization.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"source_lang": "", "manager_name": "Мария"},
    ))
multi_data = r.json()
multi_check_id = multi_data["multi_check_id"]
print("   summary:", multi_data["summary"])

r = check("multi-check history shows attribution", client.get(f"/projects/{project_id}/multi-check"))
assert r.json()[0]["performed_by_name"] == "Мария"

check("multi-check report download", client.get(
    f"/projects/{project_id}/multi-check/{multi_check_id}/report.xlsx"
))

# --- re-running migrations on an already-migrated DB should be a no-op ---
dbmod.init_db()
r = check("managers survive re-migration", client.get("/managers"))
assert len(r.json()) == 2
r = check("projects survive re-migration", client.get("/projects"))
assert len(r.json()) == 1

print("\nALL SMOKETEST CHECKS PASSED")
