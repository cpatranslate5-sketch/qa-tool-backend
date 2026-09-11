"""Quick local smoke test — exercises login, projects, languages, single
check, and a real multi-upload against the sample file the user sent.
Uses a throwaway SQLite DB (no DATABASE_URL set) and no AI key, so only
rule-based findings are expected, not AI ones."""
import os
import sys

os.environ.pop("DATABASE_URL", None)
if os.path.exists("qa_tool_smoketest.db"):
    os.remove("qa_tool_smoketest.db")
os.environ["DATABASE_URL"] = ""

sys.path.insert(0, os.path.dirname(__file__))

import app.database as dbmod
dbmod.engine = dbmod.create_engine("sqlite:///./qa_tool_smoketest.db", connect_args={"check_same_thread": False})
dbmod.SessionLocal = dbmod.sessionmaker(autocommit=False, autoflush=False, bind=dbmod.engine)

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

# health
check("health", client.get("/health"))

# login (new)
r = check("login (register)", client.post("/auth/login", json={"name": "Александр", "code": "1234"}))
manager_id = r.json()["manager_id"]

# login again with wrong code
check("login (wrong code)", client.post("/auth/login", json={"name": "Александр", "code": "wrong"}), expect=401)

# login again with correct code
check("login (correct code)", client.post("/auth/login", json={"name": "Александр", "code": "1234"}))

# create project
r = check("create project", client.post(f"/managers/{manager_id}/projects", json={"name": "Pragmatic Play Promo"}))
project_id = r.json()["id"]

# duplicate project name
check("duplicate project", client.post(f"/managers/{manager_id}/projects", json={"name": "Pragmatic Play Promo"}), expect=409)

# set glossary
check("set glossary", client.put(
    f"/managers/{manager_id}/projects/{project_id}/glossary",
    json={"glossary": "Mission Rush -> Mission Rush (не переводится)"},
))

# add language folder
r = check("add language", client.post(f"/managers/{manager_id}/projects/{project_id}/languages", json={"lang_code": "ru"}))
language_id = r.json()["id"]

# list languages
check("list languages", client.get(f"/managers/{manager_id}/projects/{project_id}/languages"))

# single check tied to project/language (numbers mismatch on purpose)
r = check("single check", client.post("/check", json={
    "source": "The bonus is $50 and expires in 3 days.",
    "translation": "Бонус составляет $500 и истекает через 3 дня.",
    "checks": ["numbers", "placeholders", "glossary", "register", "typo"],
    "project_id": project_id,
    "language_id": language_id,
}))
print("   findings:", r.json()["findings"])

# history should have 1 entry
r = check("single check history", client.get(f"/managers/{manager_id}/projects/{project_id}/languages/{language_id}/history"))
assert len(r.json()) == 1, f"expected 1 history entry, got {len(r.json())}"
print("   history entries:", len(r.json()))

# multi-check with the real sample file
sample_path = "/root/.claude/uploads/aee9e6e5-e96f-5b4b-aa4e-8aad6284c8c9/147efc1b-Promo_Rules_Localization.xlsx"
with open(sample_path, "rb") as f:
    r = check("multi-check upload", client.post(
        f"/managers/{manager_id}/projects/{project_id}/multi-check",
        files={"file": ("Promo_Rules_Localization.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"source_lang": ""},
    ))
multi_data = r.json()
print("   resolved source_lang:", multi_data["source_lang"])
print("   summary:", multi_data["summary"])
multi_check_id = multi_data["multi_check_id"]
assert multi_data["source_lang"] == "en"
assert multi_data["summary"]["languages_checked"], "expected at least one target language checked"

# multi-check detail fetch
check("multi-check detail", client.get(f"/managers/{manager_id}/projects/{project_id}/multi-check/{multi_check_id}"))

# multi-check history
r = check("multi-check history", client.get(f"/managers/{manager_id}/projects/{project_id}/multi-check"))
assert len(r.json()) == 1

# report download
r = check("multi-check report download", client.get(
    f"/managers/{manager_id}/projects/{project_id}/multi-check/{multi_check_id}/report.xlsx"
))
assert r.headers["content-type"].startswith("application/vnd.openxmlformats")
with open("/tmp/qa_report_smoketest.xlsx", "wb") as out:
    out.write(r.content)
print("   report bytes:", len(r.content))

print("\nALL SMOKETEST CHECKS PASSED")
