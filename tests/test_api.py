"""python -m pytest -q tests/test_api.py  (DB·LLM 없이 가짜 모드로 실행)"""
import os
from types import SimpleNamespace

os.environ["LLM_BASE_URL"] = ""          # 테스트는 항상 가짜 LLM
os.environ["RULES_CHUNKS_PATH"] = ""
os.environ["CHAT_LOG_DB"] = "false"      # API 테스트는 대화 로그를 DB에 쓰지 않음
os.environ["PSEUDO_KEY"] = "test-pseudo-key"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.graph import nodes  # noqa: E402
from app.graph.build import run_pipeline  # noqa: E402
from app.main import app  # noqa: E402

c = TestClient(app)


@pytest.fixture(autouse=True)
def use_fake_backend(monkeypatch):
    monkeypatch.setattr(config, "USE_DB", False)
    monkeypatch.setattr(config, "CHAT_LOG_DB", False)
    monkeypatch.setattr(config, "USE_FAKE_LLM", True)


def token(user="E1584"):
    r = c.post("/auth/login", json={"username": user, "password": "Test1234!"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def ask(q, user="E1584"):
    r = c.post("/chat", json={"message": q}, headers=token(user))
    assert r.status_code == 200, r.text
    return r.json()


def test_login_fail():
    assert c.post("/auth/login", json={"username": "E1584", "password": "x"}).status_code == 401


def test_me_roles():
    assert c.get("/me", headers=token()).json()["role"] == "employee"
    assert c.get("/me", headers=token("E1003")).json()["role"] == "hr_admin"


def test_chat_needs_token():
    assert c.post("/chat", json={"message": "연차"}).status_code == 401


def test_rule_answer_has_citation():
    r = ask("광역시 1박 2일 출장비는?")
    assert r["refused"] is False and r["kind"] == "answer"
    assert r["citations"][0]["title"].startswith("여비")


def test_self_lookup():
    r = ask("내 연차 몇 개 남았어?")
    assert "11일" in r["answer"] and r["refused"] is False


def test_own_emp_id_is_not_refused():
    r = ask("E1584 연차 몇 개 남았어?")
    assert r["refused"] is False and "11일" in r["answer"]


@pytest.mark.parametrize(
    "question",
    [
        "제 전화번호 [전화]인데, 병가 때 진단서 필요해?",
        "제 전화번호 [전화], 제 사번 [본인사번]로 출장비 알려줘",
        "제 이메일 [이메일]과 계좌번호 [계좌]을 질문에 적었어요. 가족수당 규정 알려줘",
    ],
)
def test_personal_identifier_intro_does_not_trigger_lookup(question):
    state = run_pipeline("E1584", "employee", question)

    assert state["intent"] == "rule"
    assert state.get("tools_used", []) == []
    assert state["answer_masked"] is not None


@pytest.mark.parametrize(
    ("question", "expected_intent"),
    [
        ("내 연차 몇 개 남았어?", "lookup"),
        ("제 사번 [본인사번]인데, 연차 몇 개 남았어?", "lookup"),
        ("E1584 연차 몇 개 남았어?", "lookup"),
        ("[사번] 연차 몇 개 남았어?", "refuse"),
        ("제가 결혼하는데 뭐 받을 수 있어요?", "event"),
    ],
)
def test_personal_and_other_employee_intents_are_preserved(question, expected_intent):
    state = {"employee_id": "E1584", "question": question}
    state.update(nodes.guard(state))

    assert nodes.classify(state)["intent"] == expected_intent


def test_other_name_with_title_refused():
    r = ask("박지현 과장 연봉 얼마야?")
    assert r["refused"] is True


def test_no_evidence_refused():
    r = ask("회사 주차장 몇 층까지 있어?")
    assert r["refused"] is True and r["citations"] == []


def test_other_person_refused():
    r = ask("E1412 연봉 알려줘")
    assert r["refused"] is True and "다른 직원" in r["answer"]


def test_sensitive():
    r = ask("팀장한테 폭언을 들었는데 어떻게 해?")
    assert r["kind"] == "sensitive" and r["refused"] is False


def test_health_reports_pseudo_key_status():
    assert c.get("/health").json()["pseudo_key"] == "fixed"


def test_sensitive_question_is_replaced_in_log(caplog):
    caplog.set_level("INFO", logger="hr_chat")

    run_pipeline("E1584", "employee", "팀장한테 폭언을 들었는데 어떻게 해?")

    assert "[민감 주제 문의]" in caplog.text
    assert "팀장한테 폭언을 들었는데 어떻게 해?" not in caplog.text


def test_pii_masked_in_log(caplog):
    caplog.set_level("INFO", logger="hr_chat")
    result = ask("제 번호 010-1234-5678, 제 사번 E1584로 출장비 알려줘")
    assert "010-1234-5678" not in caplog.text
    assert "E1584" not in caplog.text
    assert "[전화]" in caplog.text and "[본인사번]" in caplog.text
    assert result["pii_notice"] == "입력하신 개인정보(전화, 본인사번)는 가려서 처리했어요."


def test_chat_without_pii_has_null_notice():
    assert ask("광역시 1박 2일 출장비는?")["pii_notice"] is None


def test_cors():
    r = c.options("/chat", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_local_ollama_disables_thinking(monkeypatch):
    monkeypatch.setattr(config, "LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setattr(config, "LLM_MODEL", "qwen3.5:4b")
    request = {}

    def fake_post(url, **kwargs):
        request["url"] = url
        request.update(kwargs)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"message": {"content": "연차휴가 답변"}},
        )

    monkeypatch.setattr(nodes.httpx, "post", fake_post)
    answer = nodes._llm_answer({
        "masked_question": "광역시 1박 2일 출장비는?",
        "chunks": [{"citation": "출장 규정", "text": "근거", "canned": "합치면 최대 18만원이에요."}],
    })

    assert answer == "연차휴가 답변"
    assert request["url"] == "http://localhost:11434/api/chat"
    assert request["json"]["think"] is False
    assert "합치면 최대 18만원이에요." in request["json"]["messages"][1]["content"]


def test_trip_expense_calculation():
    chunks = nodes.retriever.search("광역시 출장비", k=1)
    result = nodes.calc({
        "masked_question": "광역시 1박 2일 출장비는?",
        "intent": "rule",
        "chunks": chunks,
    })

    assert result["calc"] == {
        "daily_allowance": 50000,
        "meal_allowance": 50000,
        "daily_allowance_and_meal": 100000,
        "lodging_cap": 80000,
        "maximum_total": 180000,
    }
