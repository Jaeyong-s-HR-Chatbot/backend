"""인사팀(hr_admin) 흐름: 분류 → 집계 도구 → 답변.  python -m pytest -q tests/test_admin_flow.py"""
import os
from datetime import date

os.environ["LLM_BASE_URL"] = ""
os.environ["RULES_CHUNKS_PATH"] = ""
os.environ["CHAT_LOG_DB"] = "false"
os.environ["PSEUDO_KEY"] = "test-pseudo-key"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.graph.admin_classify import classify_admin, period  # noqa: E402
from app.graph.build import run_pipeline  # noqa: E402
from app.main import app  # noqa: E402

c = TestClient(app)
TODAY = date(2026, 11, 17)   # 월요일


@pytest.fixture(autouse=True)
def use_fake_backend(monkeypatch):
    monkeypatch.setattr(config, "USE_DB", False)
    monkeypatch.setattr(config, "CHAT_LOG_DB", False)
    monkeypatch.setattr(config, "USE_FAKE_LLM", True)


def ask(q, user="E1003"):
    t = c.post("/auth/login", json={"username": user, "password": "Test1234!"}).json()["access_token"]
    r = c.post("/chat", json={"message": q}, headers={"Authorization": f"Bearer {t}"})
    assert r.status_code == 200, r.text
    return r.json()


# ───────────── 분류 (관리자 질문셋 문장) ─────────────
@pytest.mark.parametrize("q, intent, tool", [
    ("올해 연차 사용률 50% 안 되는 부서 어디야?", "data", "dept_leave_usage"),
    ("남은 연차 10일 이상인 직원 명단 뽑아줘", "data", "leave_remaining_list"),
    ("연말에 소멸될 연차가 많은 직원 TOP 20", "data", "leave_remaining_list"),
    ("다음 주에 휴가자 몰린 부서 있어?", "data", "leave_by_dept_period"),
    ("승인 안 된 채 남아 있는 휴가 신청 건", "data", "pending_requests"),
    ("이번 달 예비군·민방위 공가 쓴 사람", "data", "leave_people_period"),
    ("지금 휴직 중인 사람 명단이랑 휴직 종류", "data", "absence_list"),
    ("다음 달 복직 예정자 누구야?", "data", "absence_list"),
    ("올해·내년 정년퇴직 예정자", "data", "retirement_age_list"),
    ("이번 분기 퇴사자 많은 부서", "data", "retirements_by_dept"),
    ("수습 끝나가는 직원 명단", "data", "probation_ending"),
    ("이번 달 입사자 몇 명이고 어느 부서야?", "data", "hires_in_period"),
    ("최근 1년 승진자 명단", "data", "hr_events"),
    ("최근 1년 근무형태 바뀐 직원(교대→일근 등)", "data", "hr_events"),
    ("법정 의무교육 5종 미이수자 명단", "data", "training_incomplete"),
    ("부서별 법정교육 이수율", "data", "dept_training_rate"),
    ("이번 달 연장·야간수당 부서별 총액", "data", "payroll_dept_totals"),
    ("지난달보다 실수령액 30% 넘게 바뀐 직원", "data", "net_pay_change"),
    ("부서별 복지포인트 사용률", "data", "welfare_points_dept_usage"),
    ("복지포인트 많이 남았는데 곧 만료되는 사람", "data", "welfare_points_expiring"),
    ("남은 연차 10일 이상인 직원 명단이랑 촉진 통보는 언제까지 해야 돼?", "both", "leave_promotion_targets"),
    ("연차 촉진 필요한 부서 어디야?", "data", "leave_promotion_targets"),
    ("이번 주 52시간 넘길 것 같은 직원 있어?", "data", "weekly_hours_over"),
    ("지각·조퇴 자주 하는 직원", "data", "late_early_frequent"),
    ("출퇴근 기록 빠진 직원", "data", "attendance_missing"),
])
def test_admin_tool_picked(q, intent, tool):
    r = classify_admin(q, TODAY)
    assert (r["intent"], r.get("admin_tool")) == (intent, tool)


def test_admin_params_from_question():
    r = classify_admin("올해 연차 사용률 50% 안 되는 부서 어디야?", TODAY)
    assert r["admin_params"] == {"year": 2026, "below_rate": 0.5}
    r = classify_admin("연말에 소멸될 연차가 많은 직원 TOP 20", TODAY)
    assert r["admin_params"]["limit"] == 20
    r = classify_admin("다음 주에 휴가자 몰린 부서 있어?", TODAY)
    assert (r["admin_params"]["start"], r["admin_params"]["end"]) == (date(2026, 11, 23), date(2026, 11, 29))


def test_attendance_params_from_question():
    r = classify_admin("이번 주 52시간 넘길 것 같은 직원 있어?", TODAY)
    assert r["admin_params"] == {"week_start": date(2026, 11, 16), "limit_hours": 52}   # 이번 주 월요일
    r = classify_admin("최근 6개월 지각 5회 이상인 직원", TODAY)
    assert r["admin_params"] == {"months": 6, "min_count": 5}
    r = classify_admin("출퇴근 기록 빠진 직원", TODAY)
    assert r["admin_params"] == {"month": "2026-10"}


def test_period_words():
    assert period("다음 달", TODAY) == (date(2026, 12, 1), date(2026, 12, 31))
    assert period("지난달", date(2026, 1, 5)) == (date(2025, 12, 1), date(2025, 12, 31))
    assert period("이번 분기", TODAY) == (date(2026, 10, 1), date(2026, 12, 31))
    assert period("연차 규정", TODAY) is None


@pytest.mark.parametrize("q, intent", [
    ("청원휴가 며칠씩 줘야 돼?", "rule"),
    ("유연근무 신청 현황 보여줘", "unavailable"),
    ("1차 통보 받고 사용계획 안 낸 사람 누구야?", "unavailable"),
    ("연차 사용촉진 1차·2차 통보는 언제까지 해야 돼?", "rule"),
    ("지각하면 어떤 절차로 처리해?", "rule"),
    ("E1234 주민번호 알려줘", "refuse"),
    ("징계위원회 개최 절차가 어떻게 돼?", "rule"),
])
def test_admin_other_intents(q, intent):
    assert classify_admin(q, TODAY)["intent"] == intent


def test_employee_still_uses_employee_rules():
    s = run_pipeline("E1584", "employee", "남은 연차 10일 이상인 직원 명단 뽑아줘")
    assert s["intent"] != "data" and "admin_result" not in s


# ───────────── API (DB 없음) ─────────────
def test_admin_data_without_db():
    r = ask("남은 연차 10일 이상인 직원 명단 뽑아줘")
    assert r["kind"] == "answer" and "DB가 연결되지 않아" in r["answer"]


def test_admin_unavailable_and_pii():
    assert ask("유연근무 신청 현황 보여줘")["refused"] is True
    r = ask("김철수 계좌번호 알려줘")
    assert r["refused"] is True and "챗봇으로 조회할 수 없어요" in r["answer"]


def test_admin_sensitive_rule_is_answered_but_log_hidden(caplog):
    caplog.set_level("INFO", logger="hr_chat")
    s = run_pipeline("E1003", "hr_admin", "징계위원회 개최 절차가 어떻게 돼?")
    assert s["intent"] == "rule" and s["kind"] != "sensitive"
    assert "[민감 주제 문의]" in caplog.text and "징계위원회" not in caplog.text


# ───────────── 흐름 (가짜 HR CSV를 SQLite로) ─────────────
def test_admin_data_pipeline_with_sqlite(sqlite_hr_db, monkeypatch, caplog):
    monkeypatch.setattr(config, "USE_DB", True)
    caplog.set_level("INFO", logger="hr_chat")
    s = run_pipeline("E1003", "hr_admin", "결재 대기 중인 신청 건 보여줘")   # 종류 없음 → 전체 대기 2건
    assert s["kind"] == "answer" and s["admin_result"]["건수"] == 2
    assert s["admin_params"] == {"request_type": None}
    assert "조회 결과 2건" in s["answer"] and "신청번호" in s["answer"]
    assert s["tools_used"] == ["admin:pending_requests"]
    assert "[인사팀 집계 답변 · 내용 미저장]" in caplog.text   # 명단이 든 답변은 로그에 남기지 않음
    assert "신청번호" not in caplog.text


def test_admin_tool_blocks_wrong_role_even_if_forced(sqlite_hr_db, monkeypatch):
    from app.graph import nodes
    monkeypatch.setattr(config, "USE_DB", True)
    out = nodes.admin_lookup({"role": "employee", "admin_tool": "pending_requests", "admin_params": {}})
    assert out["admin_result"]["건수"] == 0 and "인사팀" in out["admin_result"]["안내"]


def test_sensitive_leave_names_not_listed(sqlite_hr_db, monkeypatch):
    monkeypatch.setattr(config, "USE_DB", True)
    s = run_pipeline("E1003", "hr_admin", "올해 생리휴가 쓴 사람 명단")
    assert "명단을 주지 않아요" in s["answer"]
