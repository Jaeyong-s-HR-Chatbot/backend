"""인사팀 분류 (임직원과 같은 순서: 민감 표시 → 다른 직원 허용 → 질문 없음 → LLM → 실패 시 규칙).

python -m pytest -q tests/test_admin_llm_classify.py
"""
import json
import os
from datetime import date

os.environ["PSEUDO_KEY"] = "test-pseudo-key"

import pytest  # noqa: E402

from app import config  # noqa: E402
from app.graph import admin_llm_classify, nodes  # noqa: E402

TODAY = date(2026, 11, 17)


@pytest.fixture(autouse=True)
def hybrid(monkeypatch):
    monkeypatch.setattr(config, "CLASSIFY_MODE", "hybrid")
    monkeypatch.setattr(config, "CALC_TOOLS", False)


def _state(question):
    s = {"employee_id": "E1003", "role": "hr_admin", "question": question}
    s.update(nodes.guard(s))
    return s


def _classify(question):
    return nodes.classify(_state(question))


def _llm(monkeypatch, intent="rule", tools=None, calc=None, admin_tool=None, admin_params=None, raw=None, calls=None):
    payload = raw if raw is not None else json.dumps(
        {"intent": intent, "tools": tools or [], "calc": calc, "admin_tool": admin_tool,
         "admin_params": admin_params or {}}, ensure_ascii=False)

    def fake(question, today):
        if calls is not None:
            calls.append(question)
        return payload
    monkeypatch.setattr(admin_llm_classify, "_request", fake)


# ───────────── 순서 ─────────────
def test_rule_mode_never_calls_llm(monkeypatch):
    monkeypatch.setattr(config, "CLASSIFY_MODE", "rule")
    monkeypatch.setattr(admin_llm_classify, "_request", lambda *a: pytest.fail("LLM called"))
    assert _classify("올해 연차 사용률 50% 안 되는 부서 어디야?")["admin_tool"] == "dept_leave_usage"


def test_sensitive_word_is_flag_not_stop(monkeypatch):
    calls = []
    _llm(monkeypatch, intent="rule", calls=calls)
    r = _classify("직장 내 괴롭힘 신고 들어오면 조사 절차가 어떻게 돼?")
    assert r["intent"] == "rule" and r["sensitive_topic"] is True and calls   # 종료하지 않고 LLM까지 감


def test_other_employee_is_allowed(monkeypatch):
    calls = []
    _llm(monkeypatch, intent="both", admin_tool="leave_people_period",
         admin_params={"start": "2026-11-01", "end": "2026-11-30", "leave_type": "연차"}, calls=calls)
    r = _classify("김민지 과장 이번 달 휴가 쓴 날짜")
    assert r["intent"] == "both" and r["admin_tool"] == "leave_people_period" and calls


def test_no_question_returns_rule_result(monkeypatch):
    monkeypatch.setattr(admin_llm_classify, "_request", lambda *a: pytest.fail("LLM called"))
    assert "admin_tool" not in _classify("제 사번은 E1003이에요.")


def test_llm_runs_even_when_rule_found_tool(monkeypatch):
    # 집계 도구를 쓸지도 LLM이 정해요. 규칙이 도구를 찾았어도 LLM 결과를 따름
    _llm(monkeypatch, intent="rule")
    r = _classify("연차 촉진 필요한 부서 어디야?")
    assert r["intent"] == "rule" and "admin_tool" not in r


# ───────────── LLM 결과 ─────────────
def test_llm_picks_admin_tool(monkeypatch):
    _llm(monkeypatch, intent="both", admin_tool="leave_remaining_list", admin_params={"year": 2026, "limit": 20})
    r = _classify("올해 휴가 제일 많이 남은 사람 20명만")
    assert r["admin_tool"] == "leave_remaining_list" and r["admin_params"] == {"year": 2026, "limit": 20}


def test_admin_tool_forces_both_and_search_hint(monkeypatch):
    _llm(monkeypatch, intent="lookup", admin_tool="weekly_hours_over", admin_params={"week_start": "2026-11-16"})
    r = _classify("이번 주 근무시간 너무 많은 사람")
    assert r["intent"] == "both" and r["admin_params"] == {"week_start": date(2026, 11, 16)} and "search_hint" in r


def test_self_lookup(monkeypatch):
    _llm(monkeypatch, intent="lookup", tools=["leave_balance"])
    r = _classify("내 연차 몇 개 남았어?")
    assert r["intent"] == "lookup" and "admin_tool" not in r and r["classifier_tools"] == ["profile", "leave_balance"]


def test_calc_slots_pass_through(monkeypatch):
    _llm(monkeypatch, intent="rule", calc={"type": "annual_leave", "slots": {"hire_date": "2019-03-02"}})
    r = _classify("입사일이 2019-03-02인 직원 올해 연차 며칠이야?")
    assert r["classifier_calc"]["type"] == "annual_leave"


def test_bad_admin_params_dropped(monkeypatch):
    _llm(monkeypatch, intent="both", admin_tool="hires_in_period",
         admin_params={"start": "11월 1일", "end": "2026-11-30", "role": "hr_admin", "sql": "DROP"})
    assert _classify("요번에 새로 온 사람들")["admin_params"] == {"end": date(2026, 11, 30)}


def test_reversed_period_dropped(monkeypatch):
    _llm(monkeypatch, intent="both", admin_tool="hires_in_period",
         admin_params={"start": "2026-12-01", "end": "2026-11-01"})
    assert _classify("요번에 새로 온 사람들")["admin_params"] == {}


def test_unknown_admin_tool_ignored(monkeypatch):
    _llm(monkeypatch, intent="rule", admin_tool="run_sql")
    r = _classify("요번에 새로 온 사람들")
    assert r["intent"] == "rule" and "admin_tool" not in r


@pytest.mark.parametrize("intent", ["refuse", "sensitive", "unavailable"])
def test_admin_has_no_refuse_sensitive_unavailable(intent, monkeypatch):
    # LLM이 인사팀에 없는 의도를 내면 응답 전체를 버리고 규칙 결과를 써요
    _llm(monkeypatch, intent=intent)
    r = _classify("김민지 과장 이번 달 휴가 쓴 날짜")
    assert r["intent"] not in ("refuse", "sensitive", "unavailable")
    assert intent not in admin_llm_classify.OUTPUT_SCHEMA["properties"]["intent"]["enum"]


@pytest.mark.parametrize("raw", ["not json", '{"intent": "data"}', "[]"])
def test_broken_response_falls_back_to_rule(raw, monkeypatch):
    _llm(monkeypatch, raw=raw)
    assert _classify("올해 연차 사용률 50% 안 되는 부서 어디야?")["admin_tool"] == "dept_leave_usage"


def test_request_error_falls_back_to_rule(monkeypatch):
    import httpx

    def boom(*a):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(admin_llm_classify, "_request", boom)
    assert _classify("올해 연차 사용률 50% 안 되는 부서 어디야?")["admin_tool"] == "dept_leave_usage"


def test_assist_keeps_rule_intent_and_tool(monkeypatch):
    monkeypatch.setattr(config, "CLASSIFY_MODE", "assist")
    _llm(monkeypatch, intent="rule", calc={"type": "annual_leave", "slots": {"service_years": 3}})
    r = _classify("올해 연차 사용률 50% 안 되는 부서 어디야?")
    assert r["admin_tool"] == "dept_leave_usage" and r["classifier_calc"]["type"] == "annual_leave"


def test_prompt_lists_every_tool():
    prompt = admin_llm_classify._system_prompt(TODAY)
    for name in admin_llm_classify.admin_tools.ADMIN_TOOLS:
        assert name in prompt
    assert "start:date" in prompt and "below_rate:float" in prompt and "leave_balance" in prompt
