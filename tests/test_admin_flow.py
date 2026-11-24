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
    ("올해 연차 사용률 50% 안 되는 부서 어디야?", "both", "dept_leave_usage"),
    ("남은 연차 10일 이상인 직원 명단 뽑아줘", "both", "leave_remaining_list"),
    ("연말에 소멸될 연차가 많은 직원 TOP 20", "both", "leave_remaining_list"),
    ("다음 주에 휴가자 몰린 부서 있어?", "both", "leave_by_dept_period"),
    ("승인 안 된 채 남아 있는 휴가 신청 건", "both", "pending_requests"),
    ("이번 달 예비군·민방위 공가 쓴 사람", "both", "leave_people_period"),
    ("지금 휴직 중인 사람 명단이랑 휴직 종류", "both", "absence_list"),
    ("다음 달 복직 예정자 누구야?", "both", "absence_list"),
    ("올해·내년 정년퇴직 예정자", "both", "retirement_age_list"),
    ("이번 분기 퇴사자 많은 부서", "both", "retirements_by_dept"),
    ("수습 끝나가는 직원 명단", "both", "probation_ending"),
    ("이번 달 입사자 몇 명이고 어느 부서야?", "both", "hires_in_period"),
    ("최근 1년 승진자 명단", "both", "hr_events"),
    ("최근 1년 근무형태 바뀐 직원(교대→일근 등)", "both", "hr_events"),
    ("법정 의무교육 5종 미이수자 명단", "both", "training_incomplete"),
    ("부서별 법정교육 이수율", "both", "dept_training_rate"),
    ("이번 달 연장·야간수당 부서별 총액", "both", "payroll_dept_totals"),
    ("지난달보다 실수령액 30% 넘게 바뀐 직원", "both", "net_pay_change"),
    ("부서별 복지포인트 사용률", "both", "welfare_points_dept_usage"),
    ("복지포인트 많이 남았는데 곧 만료되는 사람", "both", "welfare_points_expiring"),
    ("남은 연차 10일 이상인 직원 명단이랑 촉진 통보는 언제까지 해야 돼?", "both", "leave_promotion_targets"),
    ("연차 촉진 필요한 부서 어디야?", "both", "leave_promotion_targets"),
    ("이번 주 52시간 넘길 것 같은 직원 있어?", "both", "weekly_hours_over"),
    ("지각·조퇴 자주 하는 직원", "both", "late_early_frequent"),
    ("출퇴근 기록 빠진 직원", "both", "attendance_missing"),
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


def test_always_both_tools_add_search_hint():
    r = classify_admin("이번 주 52시간 넘길 것 같은 직원 있어?", TODAY)
    assert r["intent"] == "both" and "연장근로" in r["search_hint"]
    assert "search_hint" not in classify_admin("남은 연차 10일 이상인 직원 명단 뽑아줘", TODAY)


def test_period_words():
    assert period("다음 달", TODAY) == (date(2026, 12, 1), date(2026, 12, 31))
    assert period("지난달", date(2026, 1, 5)) == (date(2025, 12, 1), date(2025, 12, 31))
    assert period("이번 분기", TODAY) == (date(2026, 10, 1), date(2026, 12, 31))
    assert period("연차 규정", TODAY) is None


@pytest.mark.parametrize("q, intent", [
    ("청원휴가 며칠씩 줘야 돼?", "rule"),
    ("연차 사용촉진 1차·2차 통보는 언제까지 해야 돼?", "rule"),
    ("지각하면 어떤 절차로 처리해?", "rule"),
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


def test_admin_sensitive_rule_is_answered_but_log_hidden(caplog):
    caplog.set_level("INFO", logger="hr_chat")
    s = run_pipeline("E1003", "hr_admin", "성희롱 고충 처리 절차가 어떻게 돼?")
    assert s["intent"] == "rule" and s["kind"] != "sensitive"
    assert "[민감 주제 문의]" in caplog.text and "성희롱" not in caplog.text


def test_discipline_is_not_sensitive_word():
    """징계는 규정 질문이 많아 민감 단어가 아니에요 (임직원·인사팀 모두)"""
    from app.graph.admin_classify import classify_admin
    assert classify_admin("징계위원회 개최 절차가 어떻게 돼?", TODAY)["sensitive_topic"] is False
    s = run_pipeline("E1307", "employee", "징계 시효가 몇 년이야?")
    assert s["intent"] == "rule"


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


# ───────────── 인사팀 담당자 본인 질문 ─────────────
@pytest.mark.parametrize("q, intent", [
    ("내 연차 몇 개 남았어?", "lookup"),
    ("제 복지포인트 얼마 남았어요?", "lookup"),
    ("이번 달 내 실수령액 알려줘", "lookup"),
    ("제 휴가 신청 결재됐어요?", "lookup"),
    ("내 연차로 결혼휴가 붙여 쓸 수 있는 기준이 뭐야?", "both"),
])
def test_admin_self_questions(q, intent):
    r = classify_admin(q, TODAY)
    assert r["intent"] == intent and "admin_tool" not in r


@pytest.mark.parametrize("q, tool", [
    ("남은 연차 10일 이상인 직원 명단 뽑아줘", "leave_remaining_list"),   # '남은'만으로 본인 질문이 되지 않음
    ("내 팀원들 중에 연차 많이 남은 사람 명단", "leave_remaining_list"),   # 1인칭이어도 여러 사람
    ("제 부서 법정교육 미이수자 명단", "training_incomplete"),
])
def test_admin_group_questions_stay_aggregate(q, tool):
    assert classify_admin(q, TODAY)["admin_tool"] == tool


def test_admin_self_lookup_uses_own_record():
    s = run_pipeline("E1003", "hr_admin", "내 연차 몇 개 남았어?")
    assert s["intent"] == "lookup" and "admin_result" not in s and s.get("tools_used")


@pytest.mark.parametrize("q", [
    "휴직자 자리 결원 보충해도 돼?",
    "질병휴직 중엔 보수 얼마 줘?",
    "수습기간 줄여줄 수 있는 경우가 있어?",
    "근속연수별 복지포인트 배정 기준",
    "휴직자가 휴직 목적 외로 쓰는지 점검해야 돼?",
])
def test_rule_only_questions_do_not_pick_tool(q):
    """규정만 묻는 질문은 도구 단어(휴직자·수습·복지포인트)가 있어도 집계 도구를 안 써요"""
    r = classify_admin(q, TODAY)
    assert r["intent"] == "rule" and "admin_tool" not in r


def test_tool_picks_with_overlapping_words():
    r = classify_admin("퇴직 유형별(정년·의원 등) 인원", TODAY)
    assert r["admin_tool"] == "retirements_by_dept"
    r = classify_admin("신규입사자 입문 과정 아직 안 들은 사람", TODAY)
    assert r["admin_tool"] == "training_incomplete" and r["admin_params"]["course_type"] == "신규"
    r = classify_admin("생리휴가·보건휴가 사용 현황 부서별로", TODAY)
    assert r["admin_tool"] == "leave_by_dept_period"
    assert r["admin_params"] == {"start": date(2026, 1, 1), "end": TODAY, "leave_type": ["생리휴가", "보건휴가"]}


@pytest.mark.parametrize("q", ["안녕하세요", "네 감사합니다", "저는 인사팀 [이름]입니다.", "인사운영처 소속이에요."])
def test_greeting_and_intro_have_no_question(q):
    from app.graph.nodes import has_question_content
    assert not has_question_content(q)
    assert has_question_content("안녕하세요. 연차 사용률 낮은 부서 알려줘")


# ───────────── 집계 도구 28개: 질문별 도구 선택 (관리자 골든셋 문장) ─────────────
@pytest.mark.parametrize("q, tool", [
    ("올해 미사용 연차수당 예상 총액 얼마야?", "unused_leave_pay_estimate"),
    ("입사 1년 안 된 직원들 월 단위 연차 제대로 생기고 있어?", "new_hire_monthly_leave"),
    ("병가 연간 한도 넘긴 직원 있어?", "sick_leave_over_limit"),
    ("장기재직휴가 대상인데 아직 안 쓴 사람", "long_service_leave_unused"),
    ("야간근무 많은 직원 특수건강진단 대상자 뽑아줘", "night_work_heavy"),
    ("육아휴직 쓸 수 있는데 안 쓴 직원(만 8세 이하 자녀)", "childcare_leave_eligible_unused"),
    ("임금피크제 적용 대상자랑 몇 년차인지", "wage_peak_list"),
    ("퇴직 예정자 퇴직금 예상액", "severance_estimate"),
    ("명예퇴직 신청 가능한 직원 몇 명이야?", "honorary_retirement_eligible"),
    ("퇴직자 남은 연차 정산 금액", "retiree_leave_payout"),           # '연차+남은' 규칙(leave_remaining_list)보다 먼저
    ("출산 예정인 직원 누구야?", "expected_births"),
    ("이번 달 출산축하금 지급 대상", "birth_bonus_targets"),
    ("올해 승진 소요 최저연수 채운 사람", "promotion_min_years_met"),
    ("4급 시험승진 응시 대상자", "exam_promotion_4_candidates"),
    ("한 부서에 오래 있어서 순환전보 대상인 사람", "long_stay_same_dept"),
    ("전보제한 기간 안 지나서 못 옮기는 사람", "transfer_restricted"),
    ("올해 교육시간 제일 적은 직원", "training_hours_lowest"),
    ("가족수당 받는데 자녀가 기준 나이 넘은 사람", "family_allowance_overage"),
    ("다음 달 장기근속수당 구간 바뀌는 직원", "long_service_bracket_change"),
    ("올해 근속 10·20·30년 되는 직원", "service_anniversary"),
    ("부부가 둘 다 직원인데 가족수당 둘 다 받는 경우 있어?", "couple_family_allowance"),
    ("같은 경조사로 경조금이 두 번 나간 건 있어?", "duplicate_condolence_payments"),
    ("올해 중간에 입사·복직한 사람 복지포인트 제대로 월할됐어?", "welfare_points_proration_check"),   # 복지포인트 규칙보다 먼저
    ("이번 달 출장 많은 부서랑 예상 출장비", "trips_by_dept"),
    ("출장 끝났는데 아직 정산 안 된 건", "trips_unsettled"),
    ("같은 지역 장기 출장자 일비 감액 제대로 됐어?", "long_trip_per_diem_check"),
    ("누가 최근에 직원 개인정보 조회했어?", "audit_log_recent"),
    ("이번 달 직원들이 챗봇에 많이 물어본 주제 TOP 5 보여줘", "chat_topic_stats"),
    ("직원들은 챗봇한테 주로 뭐 물어봤어?", "chat_topic_stats"),
])
def test_new_admin_tools_picked(q, tool):
    r = classify_admin(q, TODAY)
    assert (r["intent"], r.get("admin_tool")) == ("both", tool)


@pytest.mark.parametrize("q", [
    "임금피크제 대상자 근로시간 줄여줘야 돼?",
    "명예퇴직 신청 자격이 어떻게 돼?",
    "전보제한 해제 신청 절차",
    "박지현 과장이 챗봇한테 뭐 물어봤어?",         # 특정인 대화 조회는 주제 집계가 아님
])
def test_new_tools_do_not_steal_rule_questions(q):
    assert classify_admin(q, TODAY).get("admin_tool") is None


def test_new_tool_params_from_question():
    r = classify_admin("이번 달 출장 많은 부서랑 예상 출장비", TODAY)
    assert r["admin_params"] == {"month": "2026-11"}
    r = classify_admin("다음 달 장기근속수당 구간 바뀌는 직원", TODAY)
    assert r["admin_params"] == {"month": "2026-12"}
    r = classify_admin("직원들이 챗봇에 많이 물어본 주제 TOP 3", TODAY)
    assert r["admin_params"]["limit"] == 3
    r = classify_admin("퇴직 예정자 퇴직금 예상액", TODAY)
    assert r["admin_params"] == {"retire_from": TODAY, "retire_to": date(2027, 12, 31)}


def test_new_tools_have_search_hint():
    from app.admin_tools import ADMIN_TOOLS
    from app.graph.admin_classify import SEARCH_HINTS
    new = {"unused_leave_pay_estimate", "wage_peak_list", "trips_by_dept", "service_anniversary", "duplicate_condolence_payments"}
    assert new <= set(SEARCH_HINTS) and set(SEARCH_HINTS) <= set(ADMIN_TOOLS)


@pytest.mark.parametrize("q", [
    "박지현 과장이 챗봇한테 뭐 물어봤어?",
    "괴롭힘 상담한 직원 누구야?",
    "괴롭힘 신고한 직원 명단 줘",
    "E1412가 챗봇에 뭐 물어봤는지 보여줘",
])
def test_admin_chat_log_of_person_is_refused(q):
    """대화 기록 정책: 특정인의 대화 내용·민감 상담자 신원은 인사팀에게도 거절"""
    r = classify_admin(q, TODAY)
    assert r["intent"] == "refuse" and r["refuse_reason"] == "chat_log"


@pytest.mark.parametrize("q", [
    "직원들은 챗봇한테 주로 뭐 물어봤어?",          # 주제 집계는 허용
    "괴롭힘 신고는 누구한테 해야 돼?",              # 신고 절차
    "성희롱 고충 처리 절차가 어떻게 돼?",
    "김민수 과장 연차 며칠 남았어?",                # 다른 직원 업무 조회는 허용
])
def test_admin_non_chat_log_questions_not_refused(q):
    assert classify_admin(q, TODAY)["intent"] != "refuse"


def test_admin_chat_log_refuse_message():
    from app.graph import nodes
    msg = nodes.refuse({"intent": "refuse", "refuse_reason": "chat_log"})["answer"]
    assert "대화 내용" in msg and "집계" in msg


@pytest.mark.parametrize("q, years", [
    ("올해 근속 10·20·30년 되는 직원", [10, 20, 30]),
    ("근속 25년 되는 사람 누구야", [25]),
    ("근속 10, 20년 되는 직원", [10, 20]),
])
def test_service_anniversary_years_from_question(q, years):
    r = classify_admin(q, TODAY)
    assert r["admin_tool"] == "service_anniversary" and r["admin_params"]["years"] == years


def test_service_anniversary_without_years_uses_tool_default():
    r = classify_admin("올해 근속 기념 대상 되는 직원", TODAY)
    assert r["admin_tool"] == "service_anniversary" and "years" not in r["admin_params"]


def test_eval_scores_params_by_tool_defaults():
    """채점: 비워 둔 인자는 도구 기본값으로 비교하고, 도구가 받지 않는 인자는 그대로 오답"""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import eval_admin_intent as ev
    as_of = date(2026, 11, 9)
    used = ev.effective_params("honorary_retirement_eligible", {}, as_of)
    assert used["min_service_years"] == 20 and used["min_years_to_retire"] == 1
    assert ev.effective_params("couple_family_allowance", {"pay_month": None}, as_of)["pay_month"] == "2026-10"
    assert ev.effective_params("service_anniversary", {"year": 2026}, as_of)["years"] == [10, 20, 30]
    assert "limit" not in ev.effective_params("payroll_dept_totals", {"pay_month": "2026-10"}, as_of)


@pytest.mark.parametrize("q, params", [
    ("기관사 보수교육 대상인데 안 들은 사람", {"course_name": "기관사 보수교육"}),       # A-077
    ("올해 개인정보보호 교육 미이수자", {"course_name": "개인정보보호 교육"}),
    ("승객 응대 실무 아직 안 들은 직원", {"course_name": "승객 응대 실무"}),
    ("법정 의무교육 5종 미이수자 명단", {"course_type": "법정"}),                     # 구분 단어 → 구분으로
    ("신규입사자 입문 과정 아직 안 들은 사람", {"course_type": "신규"}),
    ("교육 미이수자 명단", {"course_type": "법정"}),
])
def test_training_incomplete_course_name_or_type(q, params):
    r = classify_admin(q, TODAY)
    assert r["admin_tool"] == "training_incomplete"
    assert {k: r["admin_params"].get(k) for k in params} == params
    assert ("course_name" in r["admin_params"]) == ("course_name" in params)
