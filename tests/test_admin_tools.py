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
