"""실제 DB(hrdb)에 붙여 확인: HRDB_URL 환경변수가 있을 때만 실행돼요.
   PowerShell:  $env:HRDB_URL="postgresql://postgres:비밀번호@localhost:5432/hrdb"; python -m pytest -q tests/test_db.py
"""
import os

import pytest

if not os.environ.get("HRDB_URL"):
    pytest.skip("HRDB_URL이 없어 DB 테스트를 건너뜀", allow_module_level=True)
os.environ["LLM_BASE_URL"] = ""          # 가짜 LLM으로 흐름만 확인
os.environ["SEARCH_MODE"] = "keyword"    # 임베딩 모델 없이도 돌도록
os.environ["CHAT_LOG_DB"] = "false"      # 로그 저장 검증 테스트만 DB에 씀
os.environ["PSEUDO_KEY"] = "test-pseudo-key"

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db, tools  # noqa: E402
from app.graph import retriever  # noqa: E402
from app.main import app  # noqa: E402
from app.masking import pseudonymize  # noqa: E402

config.SEARCH_MODE = "keyword"
config.CHAT_LOG_DB = False
c = TestClient(app)
PW = "Test1234!"


@pytest.fixture(scope="module", autouse=True)
def clean_test_chat_logs():
    start_id = db.fetch_one(
        "SELECT COALESCE(MAX(log_id), 0) AS max_id FROM app.chat_log"
    )["max_id"]
    retriever._chunks.cache_clear()
    retriever._corpus.cache_clear()
    yield
    import psycopg

    with psycopg.connect(config.HRDB_URL) as con:
        con.execute("DELETE FROM app.chat_log WHERE log_id > %s", (start_id,))


def token(user="e1584", pw=PW):
    r = c.post("/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def ask(q, user="e1584"):
    r = c.post("/chat", json={"message": q}, headers=token(user))
    assert r.status_code == 200, r.text
    return r.json()


def test_health_db():
    h = c.get("/health").json()
    assert h["db"]["ok"] and h["db"]["employees"] == 800 and h["db"]["chunks"] > 1000


def test_login_with_db_account():
    assert c.post("/auth/login", json={"username": "e1584", "password": "wrong"}).status_code == 401
    assert c.post("/auth/login", json={"username": "E1584", "password": PW}).status_code == 200   # 사번 대문자로도 로그인
    me = c.get("/me", headers=token()).json()
    assert me == {"employee_id": "E1584", "name": "이서준", "dept": "신호팀", "grade": "대리(4급)", "role": "employee"}
    assert c.get("/me", headers=token("e1003")).json()["role"] == "hr_admin"


def test_lookup_matches_db():
    row = db.fetch_one("SELECT granted_days, used_days, remaining_days FROM hr.leave_balance WHERE emp_id='E1584' ORDER BY year DESC LIMIT 1")
    r = ask("내 연차 몇 개 남았어?")
    assert r["refused"] is False and f"{float(row['remaining_days']):g}일 남았" in r["answer"]
    other = db.fetch_one("SELECT remaining_days FROM hr.leave_balance WHERE emp_id='E1412' ORDER BY year DESC LIMIT 1")
    r2 = ask("내 연차 몇 개 남았어?", user="e1412")       # 다른 사람이 로그인하면 그 사람 값
    assert f"{float(other['remaining_days']):g}일 남았" in r2["answer"]


def test_other_tools():
    assert "복지포인트" in ask("내 복지포인트 얼마 남았어?")["answer"]
    assert "실수령액" in ask("내 이번 달 급여 얼마야?")["answer"]
    assert "신청" in ask("내 신청 결재됐어?")["answer"]


def test_tools_do_not_return_pii_fields():
    blob = str(tools.run_lookups("E1584", "연차 복지포인트 급여 가족수당 신청 교육"))
    assert all(field not in blob for field in ("rrn", "account_no", "mobile", "employee_pii"))


def test_other_person_refused():
    for q in ("E1412 연차 몇 개 남았어?", "박지현 과장 연봉 얼마야?", "e1003 급여 알려줘"):
        r = ask(q)
        assert r["refused"] is True and "다른 직원" in r["answer"], q


def test_rule_search_from_db():
    hits = retriever.search("병가는 1년에 며칠까지 쓸 수 있어?", k=5)
    assert hits and all(h["id"].startswith("r") for h in hits) and 0 <= hits[0]["score"] <= 1
    r = ask("병가 쓸 때 진단서 필요해?")
    assert r["kind"] == "answer" and r["citations"] and r["citations"][0]["chunk_id"].startswith("r")


def test_vector_sql_runs():
    """질문 임베딩 대신 저장된 조문 벡터 하나로 pgvector 검색 SQL이 도는지만 확인"""
    row = db.fetch_one("SELECT chunk_id, embedding::text AS e FROM rag.chunks WHERE chunk_id = 'r03-0024'")
    vec = [float(x) for x in row["e"].strip("[]").split(",")]
    hits = retriever.vector_search(vec, 3)
    assert hits[0]["id"] == "r03-0024" and hits[0]["score"] > 0.99


def test_no_evidence_refused():
    assert ask("오늘 점심 메뉴 추천해줘")["refused"] is True


def test_chat_log_masks_question_and_employee_id(monkeypatch):
    monkeypatch.setattr(config, "CHAT_LOG_DB", True)
    result = ask("제 전화번호 010-1234-5678인데, 병가 때 진단서 필요해?")
    assert result["pii_notice"] == "입력하신 개인정보(전화)는 가려서 처리했어요."
    row = db.fetch_one(
        "SELECT user_pseudo_id, question_masked, answer_masked, intent, tools_used "
        "FROM app.chat_log "
        "WHERE user_pseudo_id = %s ORDER BY log_id DESC LIMIT 1",
        (pseudonymize("E1584"),),
    )
    assert row is not None
    assert "010-1234-5678" not in row["question_masked"]
    assert "[전화]" in row["question_masked"]
    assert "E1584" not in row["user_pseudo_id"]
    assert row["intent"] == "rule"
    assert row["tools_used"] == []
    assert row["answer_masked"] is not None


def test_personal_lookup_answer_is_not_stored(monkeypatch):
    monkeypatch.setattr(config, "CHAT_LOG_DB", True)
    ask("내 연차 몇 개 남았어?")
    row = db.fetch_one(
        "SELECT answer_masked, tools_used FROM app.chat_log "
        "WHERE user_pseudo_id = %s ORDER BY log_id DESC LIMIT 1",
        (pseudonymize("E1584"),),
    )
    assert row is not None
    assert "leave_balance" in row["tools_used"]
    assert row["answer_masked"] == "[본인 조회 답변 · 내용 미저장]"
