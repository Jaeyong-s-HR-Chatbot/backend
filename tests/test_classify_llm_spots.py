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


def test_admin_llm_overrides_params_but_not_tool(monkeypatch, llm_on):
    calls = []
    monkeypatch.setattr(llm_classify, "chat_json",
                        _fake_chat({"params": {"year": 2026, "below_rate": 0.3, "unknown": 1}}, calls))
    r = classify_admin_state(_admin_state("올해 연차 사용률 30% 안 되는 부서 어디야?"), TODAY)
    assert r["admin_tool"] == "dept_leave_usage" and calls
    assert r["admin_params"]["below_rate"] == 0.3 and "unknown" not in r["admin_params"]


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
