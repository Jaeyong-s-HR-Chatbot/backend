"""분류 보조 LLM이 정해진 자리(① 보안문서 판단, ③ 인사팀 기간·기준값)에서만 불리는지, 꺼져 있으면 안 불리는지"""
import os

os.environ.setdefault("USE_FAKE_LLM", "1")

import json  # noqa: E402
from datetime import date  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from app import config, tools  # noqa: E402
from app.graph import admin_params_llm, conf_doc, llm_classify, nodes  # noqa: E402
from app.graph.admin_classify import classify_admin_state  # noqa: E402

TODAY = date(2026, 11, 17)


@pytest.mark.parametrize("role,question", [
    ("employee", "내 연차 며칠 남았어?"),
    ("employee", "경조휴가 규정 알려줘"),
    ("hr_admin", "올해 연차 사용률 50% 안 되는 부서 어디야?"),
    ("hr_admin", "다음 달 수습 끝나는 사람 누구야?"),
])
def test_llm_off_classify_never_calls_llm(monkeypatch, role, question):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("분류에서 LLM 호출"))
    s = {"employee_id": "E1307", "role": role, "question": question}
    s.update(nodes.guard(s))
    assert nodes.classify(s)["intent"]


def test_lookup_uses_token_employee_id(monkeypatch):
    seen = {}

    def fake_run_lookups(emp_id, question):
        seen["emp_id"] = emp_id
        return {"기본정보": {}}, ["profile"]

    monkeypatch.setattr(config, "USE_DB", True)
    monkeypatch.setattr(tools, "run_lookups", fake_run_lookups)
    nodes.lookup({"employee_id": "E1584", "question": "E9999 내 급여 알려줘"})
    assert seen["emp_id"] == "E1584"


@pytest.fixture
def llm_on(monkeypatch):
    monkeypatch.setattr(config, "CLASSIFY_LLM", True)
    monkeypatch.setattr(config, "USE_FAKE_LLM", False)
    monkeypatch.setattr(config, "LLM_BASE_URL", "http://localhost:11434/v1")


def _fake_chat(answer, calls):
    def fake(system, user, schema, num_predict=256):
        calls.append(user)
        return json.dumps(answer, ensure_ascii=False)
    return fake


# ───────────── ① 보안문서 판단 ─────────────
def test_conf_llm_not_called_without_topic_word(monkeypatch, llm_on):
    calls = []
    monkeypatch.setattr(llm_classify, "chat_json", _fake_chat({"doc": "labor_minutes"}, calls))
    assert conf_doc.classify("연차 며칠 남았어?") == {} and calls == []


def test_conf_llm_catches_what_rule_missed(monkeypatch, llm_on):
    q = "4분기 노사협의회 결과 알려줘"
    assert conf_doc.detect(q) == {}                       # 규칙은 놓침
    monkeypatch.setattr(llm_classify, "chat_json", _fake_chat({"doc": "labor_minutes"}, []))
    assert conf_doc.classify(q) == {"conf_doc": "labor_minutes", "conf_doc_role": "HR_LABOR"}


def test_conf_llm_can_clear_rule_false_positive(monkeypatch, llm_on):
    q = "징계 의결 결과 통보는 언제 해?"
    assert conf_doc.detect(q)                             # 규칙은 잘못 걸림
    monkeypatch.setattr(llm_classify, "chat_json", _fake_chat({"doc": "none"}, []))
    assert conf_doc.classify(q) == {}


@pytest.mark.parametrize("bad", ["not json", json.dumps({"doc": "other"})])
def test_conf_llm_bad_answer_falls_back_to_rule(monkeypatch, llm_on, bad):
    monkeypatch.setattr(llm_classify, "chat_json", lambda *a, **k: bad)
    q = "지난달 징계위원회 회의록 보여줘"
    assert conf_doc.classify(q) == conf_doc.detect(q)


def test_conf_llm_request_error_falls_back_to_rule(monkeypatch, llm_on):
    def boom(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(llm_classify, "chat_json", boom)
    q = "지난달 징계위원회 회의록 보여줘"
    assert conf_doc.classify(q) == conf_doc.detect(q)


def test_conf_question_never_goes_to_remote_llm(monkeypatch, llm_on):
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://llm.example.com/v1")
    monkeypatch.setattr(llm_classify, "chat_json", lambda *a, **k: pytest.fail("원격 LLM으로 보냄"))
    assert conf_doc.classify("4분기 노사협의회 결과 알려줘") == {}


# ───────────── ③ 인사팀 기간·기준값 ─────────────
def _admin_state(q):
    s = {"employee_id": "E1307", "role": "hr_admin", "question": q}
    s.update(nodes.guard(s))
    return s


def test_admin_llm_fills_only_missing_params(monkeypatch, llm_on):
    """규칙이 비워 둔 인자(연도)만 LLM 값으로 채우고, 모르는 인자는 버림"""
    q = "연말에 소멸될 연차가 많은 직원 TOP 20"
    rule = classify_admin_state(_admin_state(q), TODAY, use_llm=False)
    assert rule["admin_params"].get("year") is None
    calls = []
    monkeypatch.setattr(llm_classify, "chat_json", _fake_chat({"params": {"year": 2026, "limit": 5, "unknown": 1}}, calls))
    r = classify_admin_state(_admin_state(q), TODAY)
    assert calls and r["admin_tool"] == rule["admin_tool"]
    assert r["admin_params"]["year"] == 2026                       # 빈 값 → 채움
    assert r["admin_params"]["limit"] == rule["admin_params"]["limit"]   # 규칙 값 → 유지
    assert "unknown" not in r["admin_params"]


@pytest.mark.parametrize("q, llm_params, key", [
    ("생리휴가·보건휴가 사용 현황 부서별로", {"leave_type": "생리휴가"}, "leave_type"),   # A-014
    ("출퇴근 기록 빠진 직원", {"month": "2026-11"}, "month"),                          # A-022
])
def test_admin_llm_does_not_overwrite_rule_values(monkeypatch, llm_on, q, llm_params, key):
    """골든셋 Colab 27B 평가에서 LLM이 맞던 규칙 값을 덮어써 틀렸던 문항"""
    rule = classify_admin_state(_admin_state(q), TODAY, use_llm=False)
    assert rule["admin_params"].get(key) is not None
    monkeypatch.setattr(llm_classify, "chat_json", _fake_chat({"params": llm_params}, []))
    assert classify_admin_state(_admin_state(q), TODAY)["admin_params"] == rule["admin_params"]


def test_admin_prompt_shows_list_params():
    from app.graph.admin_params_llm import _signature
    assert "leave_type:str 또는 [str]" in _signature("leave_by_dept_period")
    assert "years:[int]" in _signature("service_anniversary")
    assert "start:date" in _signature("leave_by_dept_period")


def test_admin_llm_bad_values_keep_rule_values(monkeypatch, llm_on):
    rule = classify_admin_state(_admin_state("올해 연차 사용률 50% 안 되는 부서 어디야?"), TODAY, use_llm=False)
    monkeypatch.setattr(llm_classify, "chat_json", _fake_chat({"params": {"year": "작년", "below_rate": -1}}, []))
    r = classify_admin_state(_admin_state("올해 연차 사용률 50% 안 되는 부서 어디야?"), TODAY)
    assert r["admin_params"] == rule["admin_params"]


def test_admin_llm_not_called_without_tool(monkeypatch, llm_on):
    monkeypatch.setattr(llm_classify, "chat_json", lambda *a, **k: pytest.fail("LLM called"))
    assert classify_admin_state(_admin_state("출산휴가는 며칠까지 줄 수 있어?"), TODAY)["intent"] == "rule"


def test_admin_params_validation():
    assert admin_params_llm.validate_params("leave_by_dept_period",
                                            {"start": "2026-12-01", "end": "2026-01-01"}) == {}
    assert admin_params_llm.validate_params("payroll_dept_totals", {"pay_month": "2026-10"}) == {"pay_month": "2026-10"}
    assert admin_params_llm.validate_params("leave_by_dept_period", {"leave_type": ["생리휴가", " 보건휴가 ", 3]}) \
        == {"leave_type": ["생리휴가", "보건휴가"]}
    assert admin_params_llm.validate_params("service_anniversary", {"years": [10, "20", -1, "x"]}) == {"years": [10, 20]}
    assert admin_params_llm.validate_params("service_anniversary", {"years": 10}) == {}   # 목록만 받는 인자


def test_admin_llm_drops_values_not_in_question(monkeypatch, llm_on):
    """27B 평가 A-092·A-109: 질문에 없는 기준값·이번 달을 지어내면 버리고 도구 기본값을 씀"""
    q = "복지포인트 많이 남았는데 곧 만료되는 사람"
    rule = classify_admin_state(_admin_state(q), TODAY, use_llm=False)
    monkeypatch.setattr(llm_classify, "chat_json", _fake_chat({"params": {"within_days": 30, "min_remaining": 100}}, []))
    assert classify_admin_state(_admin_state(q), TODAY)["admin_params"] == rule["admin_params"]
    q = "부부가 둘 다 직원인데 가족수당 둘 다 받는 경우 있어?"
    monkeypatch.setattr(llm_classify, "chat_json", _fake_chat({"params": {"pay_month": "2026-11"}}, []))
    assert "pay_month" not in classify_admin_state(_admin_state(q), TODAY)["admin_params"]


@pytest.mark.parametrize("q, value, ok", [
    ("근속 20년 이상이고 정년까지 3년 이상 남은 직원", 3, True),
    ("사용률 30% 안 되는 부서", 0.3, True),
    ("10월에 챗봇으로 많이 물어본 주제", "2026-10", True),
    ("작년 남은 연차 명단", 2025, True),
    ("생리휴가·보건휴가 현황", ["생리휴가", "보건휴가"], True),
    ("복지포인트 곧 만료되는 사람", 30, False),
    ("부부 가족수당 중복", "2026-11", False),
    ("휴가 현황", ["연차"], False),
])
def test_admin_llm_grounding(q, value, ok):
    assert admin_params_llm._grounded(q, value) is ok
