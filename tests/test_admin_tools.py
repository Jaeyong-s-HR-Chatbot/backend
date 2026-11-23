"""인사팀 집계 도구(app/admin_tools.py) 확인.

Postgres 없이 돌도록 가짜 HR CSV를 메모리 SQLite에 올려 db.fetch_all만 바꿔 끼워요 (tests/conftest.py).
실제 Postgres 확인은 HRDB_URL을 넣고 tests/test_db.py 쪽에서 해요.
"""
from datetime import date
from pathlib import Path

import pytest

from app import admin_tools as at


@pytest.fixture(autouse=True)
def use_sqlite(sqlite_hr_db):
    return sqlite_hr_db


ASOF = date(2026, 11, 9)   # 가짜 데이터 기준일


# ───────────── 권한·실행 ─────────────
def test_employee_is_blocked():
    with pytest.raises(PermissionError):
        at.run_admin_tool("employee", "dept_leave_usage")


def test_unknown_tool():
    with pytest.raises(KeyError):
        at.run_admin_tool("hr_admin", "drop_table")


def test_unknown_params_are_dropped():
    r = at.run_admin_tool("hr_admin", "pending_requests", {"request_type": None, "sql": "DROP TABLE x"})
    assert r["건수"] == 2


def test_catalog_lists_every_tool():
    names = {t["name"] for t in at.tool_catalog()}
    assert names == set(at.ADMIN_TOOLS)
    assert all(t["description"] for t in at.tool_catalog())


def test_no_pii_columns_in_sql():
    src = Path(at.__file__).read_text(encoding="utf-8").replace(at.__doc__, "")   # 설명글 속 언급은 제외
    for word in ("employee_pii", "rrn", "account_no", "address", "mobile", "f.name"):
        assert word not in src


# ───────────── 연차·휴가 ─────────────
def test_dept_leave_usage_sorted_and_filtered():
    r = at.dept_leave_usage(2026)
    rates = [x["사용률"] for x in r["표"]]
    assert rates == sorted(rates) and len(rates) > 20
    low = at.dept_leave_usage(2026, below_rate=0.5)
    assert all(x["사용률"] < 0.5 for x in low["표"])


def test_leave_remaining_list():
    r = at.leave_remaining_list(min_days=10, limit=20)
    assert r["기준"]["연도"] == 2026 and len(r["표"]) <= 20 and r["건수"] >= len(r["표"])
    days = [x["잔여일수"] for x in r["표"]]
    assert days == sorted(days, reverse=True) and min(days) >= 10
    assert set(r["표"][0]) >= {"사번", "이름", "부서", "직급", "잔여일수"}


def test_leave_by_dept_period_counts_only():
    r = at.leave_by_dept_period("2026-11-02", "2026-11-06")
    assert r["건수"] > 0 and "이름" not in r["표"][0]
    s = at.leave_by_dept_period("2025-01-01", "2025-12-31", leave_type="생리휴가")
    assert sum(x["인원"] for x in s["표"]) > 0
    both = at.leave_by_dept_period("2025-01-01", "2025-12-31", leave_type=["생리휴가", "보건휴가"])
    assert both["기준"]["휴가종류"] == "생리휴가·보건휴가"
    assert sum(x["인원"] for x in both["표"]) >= sum(x["인원"] for x in s["표"])


def test_sensitive_leave_has_no_name_list():
    with pytest.raises(PermissionError):
        at.leave_people_period("2025-01-01", "2025-12-31", "생리휴가")
    r = at.leave_people_period("2025-01-01", "2026-12-31", "공가", limit=5)
    assert 0 < len(r["표"]) <= 5


def test_pending_requests():
    r = at.pending_requests()
    assert r["건수"] == 2 and all(x["신청번호"] for x in r["표"])


# ───────────── 휴직·퇴직·입사 ─────────────
def test_absence_list_and_returning():
    now = at.absence_list(ASOF)
    assert now["건수"] > 0 and {x["휴직종류"] for x in now["표"]} <= {"육아", "질병"}
    ret = at.absence_list(ASOF, return_from="2027-01-01", return_to="2027-01-31")
    assert all(x["종료예정"] == "2026-12-31" for x in ret["표"])


def test_retirement_age_list_dates():
    r = at.retirement_age_list(2026, 2027)
    assert r["건수"] > 0
    for x in r["표"]:
        m = int(x["생년월일"][5:7])
        assert x["정년퇴직일"].endswith("06-30" if m <= 6 else "12-31")


def test_retirements_by_dept_total():
    r = at.retirements_by_dept("2000-01-01", "2026-12-31")
    assert r["건수"] == 14


def test_probation_and_hires():
    p = at.probation_ending(ASOF, within_days=30)
    assert p["건수"] > 0 and all("2026-11-09" <= x["수습종료일"] <= "2026-12-09" for x in p["표"])
    h = at.hires_in_period("2026-10-01", "2026-10-31")
    assert sum(h["부서별"].values()) == h["건수"]


def test_hr_events_promotion():
    r = at.hr_events("승진", "2025-11-09", "2026-11-09")
    assert r["건수"] > 0 and "직급변경" in r["표"][0]


# ───────────── 근태 ─────────────
# 기대값은 RAG-hyerim eval/golden/admin_golden_set.csv (A-017, A-021, A-022)와 같아요.
def test_weekly_hours_over_falls_back_to_last_week_with_data():
    r = at.weekly_hours_over(ASOF, limit_hours=52)
    assert r["건수"] == 3 and r["기준"]["주"] == "2026-11-02~2026-11-08"
    assert r["기준"]["요청 주"] == "2026-11-09~2026-11-15" and "주의" in r
    assert all(x["연장근로"] > 12 for x in r["표"]) and sum(r["부서별"].values()) == 3
    past = at.weekly_hours_over("2026-11-04")          # 기록이 있는 주는 그대로, 주의 없음
    assert past["기준"]["주"] == "2026-11-02~2026-11-08" and "주의" not in past


def test_late_early_frequent():
    r = at.late_early_frequent(months=3, min_count=3, asof=ASOF)
    assert r["건수"] == 81 and r["기준"]["기간"] == "2026-08-10~2026-11-09"
    counts = [x["지각"] for x in r["표"]]
    assert counts == sorted(counts, reverse=True) and min(counts) >= 3


def test_attendance_missing():
    r = at.attendance_missing("2026-10")
    assert r["건수"] == 0 and r["기준"]["월"] == "2026-10"


def test_months_ago_clamps_day():
    assert at._months_ago(date(2026, 5, 31), 3) == date(2026, 2, 28)
    assert at._months_ago(date(2026, 1, 15), 1) == date(2025, 12, 15)


# ───────────── 연차 사용 촉진 ─────────────
def test_leave_promotion_targets():
    r = at.leave_promotion_targets(ASOF, min_days=10)
    assert r["단계"]["현재 단계"] == "통보 기한 지남" and r["단계"]["연차 소멸일"] == "2026-12-31"
    assert r["건수"] == sum(x["대상자"] for x in r["표"])
    assert all(x["촉진필요"] == ("예" if x["대상자"] else "아니요") for x in r["표"])
    assert all(x["잔여일수"] >= 10 for x in r["명단"]) and len(r["명단"]) <= 20
    # 1년 미만 입사자는 빠지므로 잔여 10일 이상 전체 명단보다 같거나 적어요
    assert r["건수"] <= at.leave_remaining_list(2026, min_days=10)["건수"]


def test_promotion_stage_by_date():
    assert at._promotion_stage(date(2026, 6, 30))["현재 단계"] == "1차 통보 전"
    assert at._promotion_stage(date(2026, 7, 5))["현재 단계"] == "1차 통보 기간"
    assert at._promotion_stage(date(2026, 10, 31))["현재 단계"] == "2차 통보 기간"


# ───────────── 교육·급여·복지 ─────────────
def test_training():
    inc = at.training_incomplete(course_type="법정", limit=10)
    assert inc["건수"] >= len(inc["표"]) and all(x["상태"] != "이수" for x in inc["표"])
    rate = at.dept_training_rate()
    assert [x["이수율"] for x in rate["표"]] == sorted(x["이수율"] for x in rate["표"])


def test_payroll_dept_totals_latest_month():
    r = at.payroll_dept_totals()
    assert r["기준"]["급여월"] == "2026-10"
    ot = [x["연장수당"] for x in r["표"]]
    assert ot == sorted(ot, reverse=True)


def test_net_pay_change():
    r = at.net_pay_change("2026-10", threshold=0.3)
    assert r["기준"]["비교월"] == "2026-09"
    assert all(abs(x["변화율"]) > 0.3 for x in r["표"])
    assert at._prev_month("2026-01") == "2025-12"


def test_welfare():
    u = at.welfare_points_dept_usage(2026)
    assert u["건수"] > 0
    e = at.welfare_points_expiring(ASOF, within_days=60, min_remaining=1)
    assert all(x["잔여포인트"] >= 1 and x["만료일"] <= "2027-01-08" for x in e["표"])


# ───────────── 집계 도구 28개 (골든셋 정답과 같은 값이 나오는지) ─────────────
def _ids(r):
    return set(r["ids"])


def test_unused_leave_pay_estimate():
    r = at.unused_leave_pay_estimate(2026)
    assert (r["건수"], r["총액"]) == (784, 1045152932)


def test_new_hire_monthly_leave():
    r = at.new_hire_monthly_leave(ASOF)
    assert r["건수"] == 64 and r["표"][0] == {"사번": "E1737", "이름": "김지훈", "부서": "기획팀", "직급": "5급",
                                           "입사일": "2025-12-01", "기대발생일수": 11, "실제부여일수": 15.0}


def test_sick_and_long_service_leave():
    assert at.sick_leave_over_limit(2026)["건수"] == 0
    assert at.sick_leave_over_limit(2026, limit_days=2)["건수"] > 0        # 한도를 낮추면 잡힘
    r = at.long_service_leave_unused(2026, 10)
    assert r["건수"] == 348 and r["표"][0]["근속연수"] == 35


def test_childcare_leave_eligible_unused_has_no_child_name():
    r = at.childcare_leave_eligible_unused(ASOF, 8)
    assert r["건수"] == 154 and r["표"][0]["자녀나이"] == [8]
    assert set(r["표"][0]) == {"사번", "이름", "부서", "직급", "자녀나이"}


def test_retiree_leave_payout():
    r = at.retiree_leave_payout(2026)
    assert r["건수"] == 12 and r["표"][0]["정산금액"] == 845167 and r["총액"] == sum(x["정산금액"] for x in r["표"])


def test_retirement_tools():
    wp = at.wage_peak_list(ASOF)
    assert wp["건수"] == 23 and wp["표"][0]["년차"] == 2 and wp["표"][0]["정년일"] == "2027-06-30"
    sv = at.severance_estimate(ASOF, date(2027, 12, 31))
    assert sv["건수"] == 16 and sv["표"][0]["예상퇴직금"] == 308168886 and sv["표"][0]["평균임금일액"] == 287337
    assert at.honorary_retirement_eligible(ASOF)["건수"] == 164


def test_births():
    r = at.expected_births(ASOF)
    assert _ids(r) == {"E1397", "E1412", "E1738"} and r["표"][0]["사번"] == "E1738"   # 휴가 시작이 빠른 순
    assert at.birth_bonus_targets("2026-11")["건수"] == 0       # 출생 예정(가족 등록 전)은 대상 아님
    assert at.birth_bonus_targets("2026-09")["건수"] == 0       # 9월 출생은 모두 지급 완료


def test_night_work_heavy_threshold():
    assert at.night_work_heavy(6)["건수"] == 0
    r = at.night_work_heavy(6, min_days_per_month=0.5)
    assert r["건수"] > 0 and all(x["야간근로일수"] >= 3 for x in r["표"])


def test_promotion_and_transfer_tools():
    p = at.promotion_min_years_met(2026)
    assert p["건수"] == 239 and p["직급별"] == {"3급": 50, "2급": 126, "4급": 28, "5급": 35}
    assert at.exam_promotion_4_candidates(ASOF)["건수"] == 35
    assert at.long_stay_same_dept(ASOF, 5)["건수"] == 459
    t = at.transfer_restricted(ASOF)
    assert t["계산불가"] is True and "채용 구분" in t["주의"]       # 0명이라고 단정하지 않고 못 센다고 알림


def test_training_hours_lowest():
    r = at.training_hours_lowest(2026, 10)
    hours = [x["교육시간"] for x in r["표"]]
    assert r["건수"] == 10 and hours == sorted(hours) and r["최소시간"] == 0.0 and r["표"][0]["사번"] == "E1403"


def test_allowance_tools():
    assert at.family_allowance_overage(ASOF, 19)["건수"] == 5
    b = at.long_service_bracket_change("2026-12")
    assert b["건수"] == 6 and b["표"][0]["새구간"] == "15년 이상"
    assert at.couple_family_allowance("2026-10")["건수"] == 0
    w = at.welfare_points_proration_check(2026)
    assert w["건수"] == 69 and w["불일치_ids"] == ["E1273", "E1290", "E1313", "E1417", "E1455", "E1466", "E1482", "E1520", "E1550", "E1561"]
    assert at.service_anniversary(2026)["건수"] == 57


def test_trip_and_condolence_tools():
    t = at.trips_by_dept("2026-11")
    assert t["건수"] == 22 and t["표"][0] == {"부서": "역무1센터", "출장건수": 4, "예상출장비": 1100000}
    assert at.trips_unsettled(ASOF)["건수"] == 0
    lt = at.long_trip_per_diem_check(15)
    assert lt["건수"] == 9 and {x["출장번호"]: x["일비감액"] for x in lt["표"]}["T00107"] == 87000
    assert at.duplicate_condolence_payments()["건수"] == 0


def test_audit_log_empty_is_reported():
    r = at.audit_log_recent("개인정보 조회")
    assert r["건수"] == 0 and "기록이 없어요" in r["주의"]


def test_chat_topic_stats_counts_only_topics(sqlite_hr_db):
    sqlite_hr_db.execute("ATTACH DATABASE ':memory:' AS app")
    sqlite_hr_db.execute("CREATE TABLE app.chat_log (log_id INTEGER, created_at TEXT, question_masked TEXT, intent TEXT, kind TEXT)")
    rows = [(1, "2026-11-02", "내 연차 며칠 남았어?", "lookup", "data"), (2, "2026-11-03", "연차 규정 알려줘", "rule", "rule"),
            (3, "2026-11-04", "연차 이월되나요?", "rule", "rule"), (4, "2026-10-30", "지난달 질문", "rule", "rule")]
    sqlite_hr_db.executemany("INSERT INTO app.chat_log VALUES (?, ?, ?, ?, ?)", rows)
    r = at.chat_topic_stats("2026-11", 5)
    assert r["표"][0] == {"주제": "rule", "종류": "rule", "질문수": 2} and r["건수"] == 2
    assert "연차" not in str(r["표"])                         # 질문 내용은 안 나감
