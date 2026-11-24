"""인사팀(hr_admin) 전용 집계·명단 조회 도구. 정해진 SQL만 돌려요 (LLM이 SQL을 쓰지 않음).

- 호출은 run_admin_tool()로만 해요. 역할(role)은 로그인 토큰에서 온 값이어야 하고, hr_admin이 아니면 막아요.
- 주민번호·계좌·주소(hr.employee_pii)와 가족 이름은 결과에 넣지 않아요 (couple_family_allowance만 배우자 이름·생년월일을 직원과 맞춰 보는 데 쓰고 바로 버려요). 명단에는 사번·이름·부서·직급까지만 넣어요.
- 생리·보건휴가처럼 민감한 휴가는 명단 없이 인원수만 돌려줘요.
- 규정 기준(정년 나이, 기준 비율 등)은 인자로 받아요. 값은 규정 검색 결과나 rules.json에서 정해 넘겨요.
- 결과는 {"기준": 조회 조건, "건수": n, "표": [...]} 모양이고, 명단이 길면 limit까지만 담고 "건수"에 전체 수를 적어요.
- 관리자 질문셋(질문셋/관리자_질문셋_v2.csv)의 데이터 질문 번호를 함수마다 적어 뒀어요.
"""
import inspect
import json
from datetime import date, timedelta

from . import db

SENSITIVE_LEAVE = ("생리휴가", "보건휴가", "난임치료휴가")   # 명단을 돌려주지 않는 휴가 종류
_DEPT = "LEFT JOIN hr.departments d ON d.dept_id = e.dept_id"
_ACTIVE = "e.status <> '퇴직'"


# ───────────── 공통 ─────────────
def _d(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _num(v):
    return None if v is None else round(float(v), 2)


def _date(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def _result(cond: dict, rows: list[dict], total: int | None = None) -> dict:
    cond = {k: _d(v) for k, v in cond.items() if v is not None}
    return {"기준": cond, "건수": len(rows) if total is None else total, "표": rows}


def _latest_year(table: str) -> int:
    r = db.fetch_one(f"SELECT max(year) AS y FROM hr.{table}")
    return int(r["y"]) if r and r["y"] is not None else date.today().year


def _person(r: dict) -> dict:
    return {"사번": r["emp_id"], "이름": r["name"], "부서": r["dept_name"], "직급": f"{r['grade']}급"}


def _prev_month(ym: str) -> str:
    y, m = map(int, ym.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


# ───────────── 연차·휴가 ─────────────
def dept_leave_usage(year: int | None = None, below_rate: float | None = None) -> dict:
    """부서별 연차 사용률·평균 사용일수 (사용률 낮은 순). below_rate=0.5면 50% 미만 부서만. [1, 7]"""
    year = year or _latest_year("leave_balance")
    rows = db.fetch_all(
        "SELECT d.dept_name, count(*) AS n, sum(lb.granted_days + lb.carried_days) AS granted, sum(lb.used_days) AS used "
        f"FROM hr.leave_balance lb JOIN hr.employees e ON e.emp_id = lb.emp_id {_DEPT} "
        f"WHERE lb.year = %s AND {_ACTIVE} GROUP BY d.dept_name", (year,))
    out = []
    for r in rows:
        rate = float(r["used"]) / float(r["granted"]) if r["granted"] else 0.0
        if below_rate is not None and rate >= below_rate:
            continue
        out.append({"부서": r["dept_name"], "인원": r["n"], "사용률": round(rate, 3),
                    "평균사용일수": round(float(r["used"]) / r["n"], 1)})
    out.sort(key=lambda x: x["사용률"])
    return _result({"연도": year, "사용률_미만": below_rate}, out)


def leave_remaining_list(year: int | None = None, min_days: float = 10, limit: int = 50) -> dict:
    """남은 연차가 min_days 이상인 직원 명단 (많은 순). 연차 촉진 대상·소멸 예정 TOP N. [2, 8]"""
    year = year or _latest_year("leave_balance")
    where = (f"FROM hr.leave_balance lb JOIN hr.employees e ON e.emp_id = lb.emp_id {_DEPT} "
             f"WHERE lb.year = %s AND lb.remaining_days >= %s AND {_ACTIVE}")
    total = db.fetch_one(f"SELECT count(*) AS n {where}", (year, min_days))["n"]
    rows = db.fetch_all(f"SELECT e.emp_id, e.name, d.dept_name, e.grade, lb.remaining_days, lb.used_days, lb.asof_date {where} "
                        "ORDER BY lb.remaining_days DESC, e.emp_id LIMIT %s", (year, min_days, limit))
    out = [{**_person(r), "잔여일수": _num(r["remaining_days"]), "사용일수": _num(r["used_days"]), "기준일": _d(r["asof_date"])}
           for r in rows]
    return _result({"연도": year, "잔여일수_이상": min_days}, out, total)


def leave_by_dept_period(start: date, end: date, leave_type: str | list[str] | None = None) -> dict:
    """기간 안에 휴가 쓰는(쓴) 인원을 부서별로. 휴가자 몰린 부서, 공가·생리휴가 사용 현황. 인원수만 돌려줌. [10, 14, 30]
    leave_type은 하나 또는 여러 개 ("생리휴가·보건휴가 사용 현황")."""
    start, end = _date(start), _date(end)
    sql = (f"SELECT d.dept_name, count(DISTINCT lh.emp_id) AS n, sum(lh.days) AS days "
           f"FROM hr.leave_history lh JOIN hr.employees e ON e.emp_id = lh.emp_id {_DEPT} "
           "WHERE lh.status = '승인' AND lh.start_date <= %s AND lh.end_date >= %s")
    params: tuple = (end, start)
    types = [leave_type] if isinstance(leave_type, str) else list(leave_type or [])
    if types:
        sql += f" AND lh.leave_type IN ({', '.join(['%s'] * len(types))})"
        params += tuple(types)
    rows = db.fetch_all(sql + " GROUP BY d.dept_name ORDER BY n DESC, d.dept_name", params)
    out = [{"부서": r["dept_name"], "인원": r["n"], "휴가일수": _num(r["days"])} for r in rows]
    return _result({"시작": start, "끝": end, "휴가종류": "·".join(types) or None}, out)


def leave_people_period(start: date, end: date, leave_type: str, limit: int = 50) -> dict:
    """기간 안에 특정 휴가를 쓴 직원 명단 (예: 공가). 민감 휴가는 명단을 주지 않음. [30]"""
    if leave_type in SENSITIVE_LEAVE:
        raise PermissionError(f"{leave_type}는 민감정보라 명단을 주지 않아요. leave_by_dept_period로 인원수만 보세요.")
    start, end = _date(start), _date(end)
    rows = db.fetch_all(
        f"SELECT e.emp_id, e.name, d.dept_name, e.grade, lh.start_date, lh.end_date, lh.days "
        f"FROM hr.leave_history lh JOIN hr.employees e ON e.emp_id = lh.emp_id {_DEPT} "
        "WHERE lh.status = '승인' AND lh.leave_type = %s AND lh.start_date <= %s AND lh.end_date >= %s "
        "ORDER BY lh.start_date, e.emp_id LIMIT %s", (leave_type, end, start, limit))
    out = [{**_person(r), "시작": _d(r["start_date"]), "끝": _d(r["end_date"]), "일수": _num(r["days"])} for r in rows]
    return _result({"시작": start, "끝": end, "휴가종류": leave_type}, out)


def pending_requests(request_type: str | None = None, limit: int = 50) -> dict:
    """결재 대기 중인 신청 (오래된 순). request_type 예: 휴가신청, 경조금신청, 출장신청. [15, 95]"""
    sql = (f"SELECT r.request_id, r.request_type, r.event_type, r.created_at, e.emp_id, e.name, d.dept_name, e.grade "
           f"FROM hr.requests r JOIN hr.employees e ON e.emp_id = r.emp_id {_DEPT} WHERE r.status = '대기'")
    params: tuple = ()
    if request_type:
        sql += " AND r.request_type = %s"
        params += (request_type,)
    rows = db.fetch_all(sql + " ORDER BY r.created_at, r.request_id LIMIT %s", params + (limit,))
    out = [{"신청번호": r["request_id"], "종류": r["request_type"], "내용": r["event_type"], "신청일": _d(r["created_at"]),
            **_person(r)} for r in rows]
    return _result({"신청종류": request_type}, out)


# ───────────── 휴직·복직·퇴직 ─────────────
def absence_list(asof: date | None = None, return_from: date | None = None, return_to: date | None = None) -> dict:
    """asof 기준 휴직 중인 직원과 휴직 종류. return_from~return_to를 주면 그 사이 복직 예정자만. [31, 32, 102]"""
    asof = _date(asof or date.today())
    sql = (f"SELECT e.emp_id, e.name, d.dept_name, e.grade, a.absence_type, a.start_date, a.end_date "
           f"FROM hr.absence_history a JOIN hr.employees e ON e.emp_id = a.emp_id {_DEPT} "
           "WHERE a.start_date <= %s AND a.return_date IS NULL AND a.end_date >= %s")
    params: tuple = (asof, asof)
    if return_from and return_to:
        sql += " AND a.end_date >= %s AND a.end_date <= %s"
        params += (_date(return_from) - timedelta(days=1), _date(return_to) - timedelta(days=1))
    rows = db.fetch_all(sql + " ORDER BY a.end_date, e.emp_id", params)
    out = [{**_person(r), "휴직종류": r["absence_type"], "시작": _d(r["start_date"]), "종료예정": _d(r["end_date"])} for r in rows]
    return _result({"기준일": asof, "복직_시작": return_from, "복직_끝": return_to}, out)


def retirement_age_list(from_year: int, to_year: int | None = None, retire_age: int = 60) -> dict:
    """정년퇴직 예정자. 퇴직일은 1~6월생 6/30, 7~12월생 12/31 (규정이 다르면 이 계산을 바꿔요). [38]"""
    to_year = to_year or from_year
    rows = db.fetch_all(
        f"SELECT e.emp_id, e.name, d.dept_name, e.grade, e.birth_date FROM hr.employees e {_DEPT} "
        f"WHERE {_ACTIVE} AND e.birth_date >= %s AND e.birth_date <= %s ORDER BY e.birth_date, e.emp_id",
        (date(from_year - retire_age, 1, 1), date(to_year - retire_age, 12, 31)))
    out = []
    for r in rows:
        b = _date(r["birth_date"])
        y = b.year + retire_age
        out.append({**_person(r), "생년월일": _d(b), "정년퇴직일": _d(date(y, 6, 30) if b.month <= 6 else date(y, 12, 31))})
    return _result({"연도": f"{from_year}~{to_year}", "정년": retire_age}, out)


def retirements_by_dept(start: date, end: date) -> dict:
    """기간 안 퇴직자를 부서별·퇴직유형별로. [45, 46]"""
    start, end = _date(start), _date(end)
    rows = db.fetch_all(
        f"SELECT d.dept_name, e.retire_type, count(*) AS n FROM hr.employees e {_DEPT} "
        "WHERE e.retire_date >= %s AND e.retire_date <= %s GROUP BY d.dept_name, e.retire_type ORDER BY n DESC, d.dept_name",
        (start, end))
    out = [{"부서": r["dept_name"], "퇴직유형": r["retire_type"] or "미기재", "인원": r["n"]} for r in rows]
    return _result({"시작": start, "끝": end}, out, sum(r["인원"] for r in out))


# ───────────── 입사·승진·전보 ─────────────
def probation_ending(asof: date | None = None, within_days: int = 30) -> dict:
    """수습 종료일이 asof부터 within_days 안에 오는 직원. [59]"""
    asof = _date(asof or date.today())
    rows = db.fetch_all(
        f"SELECT e.emp_id, e.name, d.dept_name, e.grade, e.hire_date, e.probation_end_date FROM hr.employees e {_DEPT} "
        f"WHERE {_ACTIVE} AND e.probation_end_date >= %s AND e.probation_end_date <= %s ORDER BY e.probation_end_date, e.emp_id",
        (asof, asof + timedelta(days=within_days)))
    out = [{**_person(r), "입사일": _d(r["hire_date"]), "수습종료일": _d(r["probation_end_date"])} for r in rows]
    return _result({"기준일": asof, "며칠안": within_days}, out)


def hires_in_period(start: date, end: date) -> dict:
    """기간 안 입사자 명단 (부서별 인원은 표에서 묶어 봄). [73]"""
    start, end = _date(start), _date(end)
    rows = db.fetch_all(
        f"SELECT e.emp_id, e.name, d.dept_name, e.grade, e.hire_date FROM hr.employees e {_DEPT} "
        "WHERE e.hire_date >= %s AND e.hire_date <= %s ORDER BY d.dept_name, e.emp_id", (start, end))
    out = [{**_person(r), "입사일": _d(r["hire_date"])} for r in rows]
    by_dept: dict[str, int] = {}
    for r in out:
        by_dept[r["부서"]] = by_dept.get(r["부서"], 0) + 1
    return {**_result({"시작": start, "끝": end}, out), "부서별": by_dept}


def hr_events(event_type: str, start: date, end: date, limit: int = 100) -> dict:
    """인사 이력 명단. event_type: 승진 / 전보 / 근무형태변경 / 휴직 / 복직 / 퇴직 / 입사. [27, 70]"""
    start, end = _date(start), _date(end)
    rows = db.fetch_all(
        "SELECT e.emp_id, e.name, d.dept_name, e.grade, h.effective_date, h.from_dept, h.to_dept, h.from_grade, h.to_grade, "
        f"h.from_work_pattern, h.to_work_pattern FROM hr.hr_history h JOIN hr.employees e ON e.emp_id = h.emp_id {_DEPT} "
        "WHERE h.event_type = %s AND h.effective_date >= %s AND h.effective_date <= %s ORDER BY h.effective_date, e.emp_id LIMIT %s",
        (event_type, start, end, limit))
    out = []
    for r in rows:
        row = {**_person(r), "발령일": _d(r["effective_date"])}
        if r["from_grade"] is not None or r["to_grade"] is not None:
            row["직급변경"] = f"{r['from_grade']}급→{r['to_grade']}급"
        if r["from_dept"] or r["to_dept"]:
            row["부서변경"] = f"{r['from_dept'] or '-'}→{r['to_dept'] or '-'}"
        if r["from_work_pattern"] or r["to_work_pattern"]:
            row["근무형태변경"] = f"{r['from_work_pattern'] or '-'}→{r['to_work_pattern'] or '-'}"
        out.append(row)
    return _result({"종류": event_type, "시작": start, "끝": end}, out)


# ───────────── 교육 ─────────────
def training_incomplete(year: int | None = None, course_type: str = "법정", course_name: str | None = None,
                        limit: int = 100) -> dict:
    """필수 교육 미이수자 명단 (과정별). course_type: 법정 / 직무 / 승진 / 신규 / 리더십. [74, 77, 82]
    course_name을 주면 그 과정(띄어쓰기 무시)의 대상자 중 미이수자만 봐요 ("기관사 보수교육"). 이때 구분·필수 여부는 안 따져요."""
    year = year or _latest_year("training")
    base = f"FROM hr.training t JOIN hr.employees e ON e.emp_id = t.emp_id {_DEPT} WHERE t.year = %s AND t.status <> '이수' AND {_ACTIVE}"
    if course_name:
        where, args, cond = (base + " AND replace(t.course_name, ' ', '') LIKE %s",
                             (year, "%" + course_name.replace(" ", "") + "%"), {"연도": year, "과정": course_name})
    else:
        where, args, cond = (base + " AND t.course_type = %s AND t.required = 'Y'",
                             (year, course_type), {"연도": year, "교육구분": course_type})
    total = db.fetch_one(f"SELECT count(*) AS n {where}", args)["n"]
    rows = db.fetch_all(f"SELECT e.emp_id, e.name, d.dept_name, e.grade, t.course_name, t.status {where} "
                        "ORDER BY t.course_name, d.dept_name, e.emp_id LIMIT %s", (*args, limit))
    out = [{**_person(r), "과정": r["course_name"], "상태": r["status"]} for r in rows]
    return _result(cond, out, total)


def dept_training_rate(year: int | None = None, course_type: str = "법정") -> dict:
    """부서별 필수 교육 이수율 (낮은 순). [75]"""
    year = year or _latest_year("training")
    rows = db.fetch_all(
        "SELECT d.dept_name, count(*) AS n, sum(CASE WHEN t.status = '이수' THEN 1 ELSE 0 END) AS done "
        f"FROM hr.training t JOIN hr.employees e ON e.emp_id = t.emp_id {_DEPT} "
        f"WHERE t.year = %s AND t.course_type = %s AND t.required = 'Y' AND {_ACTIVE} GROUP BY d.dept_name", (year, course_type))
    out = sorted(({"부서": r["dept_name"], "대상건수": r["n"], "이수건수": r["done"], "이수율": round(r["done"] / r["n"], 3)}
                  for r in rows), key=lambda x: x["이수율"])
    return _result({"연도": year, "교육구분": course_type}, out)


# ───────────── 급여·수당 ─────────────
def payroll_dept_totals(pay_month: str | None = None) -> dict:
    """부서별 연장·야간·휴일 근무시간과 수당 총액 (연장수당 많은 순). pay_month 예: '2026-10'. [18, 84]"""
    pay_month = pay_month or db.fetch_one("SELECT max(pay_month) AS m FROM hr.payroll")["m"]
    rows = db.fetch_all(
        "SELECT d.dept_name, count(*) AS n, sum(p.overtime_hours) AS ot_h, sum(p.overtime_pay) AS ot, "
        "sum(p.night_hours) AS nt_h, sum(p.night_pay) AS nt, sum(p.holiday_hours) AS hd_h, sum(p.holiday_pay) AS hd "
        f"FROM hr.payroll p JOIN hr.employees e ON e.emp_id = p.emp_id {_DEPT} "
        "WHERE p.pay_month = %s GROUP BY d.dept_name ORDER BY ot DESC, d.dept_name", (pay_month,))
    out = [{"부서": r["dept_name"], "인원": r["n"], "연장시간": _num(r["ot_h"]), "연장수당": int(r["ot"]),
            "야간시간": _num(r["nt_h"]), "야간수당": int(r["nt"]), "휴일시간": _num(r["hd_h"]), "휴일수당": int(r["hd"])}
           for r in rows]
    return _result({"급여월": pay_month}, out)


def net_pay_change(pay_month: str | None = None, threshold: float = 0.3, limit: int = 50) -> dict:
    """전월 대비 실수령액이 threshold(0.3=30%) 넘게 바뀐 직원 (변화 큰 순). [91]"""
    pay_month = pay_month or db.fetch_one("SELECT max(pay_month) AS m FROM hr.payroll")["m"]
    prev = _prev_month(pay_month)
    rows = db.fetch_all(
        "SELECT e.emp_id, e.name, d.dept_name, e.grade, a.net_pay AS now_pay, b.net_pay AS prev_pay "
        "FROM hr.payroll a JOIN hr.payroll b ON b.emp_id = a.emp_id AND b.pay_month = %s "
        f"JOIN hr.employees e ON e.emp_id = a.emp_id {_DEPT} WHERE a.pay_month = %s AND b.net_pay > 0", (prev, pay_month))
    out = []
    for r in rows:
        ch = (float(r["now_pay"]) - float(r["prev_pay"])) / float(r["prev_pay"])
        if abs(ch) > threshold:
            out.append({**_person(r), "전월실수령": int(r["prev_pay"]), "당월실수령": int(r["now_pay"]), "변화율": round(ch, 3)})
    out.sort(key=lambda x: -abs(x["변화율"]))
    return _result({"급여월": pay_month, "비교월": prev, "변화율_초과": threshold}, out[:limit], len(out))


# ───────────── 복지 ─────────────
def welfare_points_dept_usage(year: int | None = None) -> dict:
    """부서별 복지포인트 사용률 (낮은 순). [93]"""
    year = year or _latest_year("welfare_points")
    rows = db.fetch_all(
        "SELECT d.dept_name, count(*) AS n, sum(w.granted_points) AS g, sum(w.used_points) AS u "
        f"FROM hr.welfare_points w JOIN hr.employees e ON e.emp_id = w.emp_id {_DEPT} "
        f"WHERE w.year = %s AND {_ACTIVE} GROUP BY d.dept_name", (year,))
    out = sorted(({"부서": r["dept_name"], "인원": r["n"], "부여": int(r["g"]), "사용": int(r["u"]),
                   "사용률": round(float(r["u"]) / float(r["g"]), 3) if r["g"] else 0.0} for r in rows), key=lambda x: x["사용률"])
    return _result({"연도": year}, out)


def welfare_points_expiring(asof: date | None = None, within_days: int = 60, min_remaining: int = 300000,
                            limit: int = 50) -> dict:
    """만료일이 within_days 안인데 남은 포인트가 min_remaining 이상인 직원 (많이 남은 순). [92]"""
    asof = _date(asof or date.today())
    where = (f"FROM hr.welfare_points w JOIN hr.employees e ON e.emp_id = w.emp_id {_DEPT} "
             f"WHERE w.expires_on >= %s AND w.expires_on <= %s AND w.granted_points - w.used_points >= %s AND {_ACTIVE}")
    params = (asof, asof + timedelta(days=within_days), min_remaining)
    total = db.fetch_one(f"SELECT count(*) AS n {where}", params)["n"]
    rows = db.fetch_all(f"SELECT e.emp_id, e.name, d.dept_name, e.grade, w.granted_points, w.used_points, w.expires_on {where} "
                        "ORDER BY w.granted_points - w.used_points DESC, e.emp_id LIMIT %s", params + (limit,))
    out = [{**_person(r), "잔여포인트": int(r["granted_points"]) - int(r["used_points"]), "만료일": _d(r["expires_on"])}
           for r in rows]
    return _result({"기준일": asof, "며칠안": within_days, "잔여_이상": min_remaining}, out, total)


# ───────────── 근태(출퇴근 기록) ─────────────
# hr.attendance: 일근자 평일 출퇴근 기록. status = 정상 / 지각 / 오전반차 / 오후반차 (조퇴 표시는 없음).
# overtime_hours는 하루 연장근로(18시 이후), night_hours는 그중 22시 이후라 연장에 포함돼요.
_REGULAR_WEEK_HOURS = 40      # 근로기준법 제50조 1주 법정근로
_HALF_DAY = ("오전반차", "오후반차")


def _months_ago(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    m += 1
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, last))


def _attendance_last_day() -> date | None:
    r = db.fetch_one("SELECT max(work_date) AS d FROM hr.attendance")
    return _date(r["d"]) if r and r["d"] else None


def weekly_hours_over(week_start: date | None = None, limit_hours: float = 52, limit: int = 50) -> dict:
    """한 주(월~일) 연장근로가 limit_hours-40시간(52면 12시간)을 넘은 직원. 그 주 기록이 없으면 기록이 있는 마지막 주. [17]"""
    monday = _date(week_start or date.today())
    monday -= timedelta(days=monday.weekday())
    cond: dict = {}
    last = _attendance_last_day()
    if last and last < monday:                       # 아직 기록이 없는 주 → 기록이 있는 마지막 주로
        cond["요청 주"] = f"{monday}~{monday + timedelta(days=6)}"
        monday = last - timedelta(days=last.weekday())
    sunday = monday + timedelta(days=6)
    ot_limit = limit_hours - _REGULAR_WEEK_HOURS
    rows = db.fetch_all(
        f"SELECT e.emp_id, e.name, d.dept_name, e.grade, a.status, a.overtime_hours, a.night_hours "
        f"FROM hr.attendance a JOIN hr.employees e ON e.emp_id = a.emp_id {_DEPT} "
        f"WHERE a.work_date >= %s AND a.work_date <= %s AND {_ACTIVE}", (monday, sunday))
    week: dict[str, dict] = {}
    for r in rows:
        w = week.setdefault(r["emp_id"], {"r": r, "ot": 0.0, "night": 0.0, "work": 0.0, "days": 0})
        ot = float(r["overtime_hours"] or 0)
        w["ot"] += ot
        w["night"] += float(r["night_hours"] or 0)
        w["work"] += (4 if r["status"] in _HALF_DAY else 8) + ot
        w["days"] += 1
    over = sorted((w for w in week.values() if w["ot"] > ot_limit), key=lambda w: (-w["ot"], w["r"]["emp_id"]))
    out = [{**_person(w["r"]), "연장근로": round(w["ot"], 1), "야간근로": round(w["night"], 1),
            "주근로시간": round(w["work"], 1), "근무일수": w["days"]} for w in over]
    by_dept: dict[str, int] = {}
    for x in out:
        by_dept[x["부서"]] = by_dept.get(x["부서"], 0) + 1
    result = {**_result({**cond, "주": f"{monday}~{sunday}", "주 한도": limit_hours, "연장근로_초과": ot_limit},
                        out[:limit], len(out)), "부서별": by_dept}
    if "요청 주" in cond:
        result["주의"] = f"출퇴근 기록이 {last}까지라 요청한 주는 아직 기록이 없어요. 기록이 있는 마지막 주 기준이에요."
    return result


def late_early_frequent(months: int = 3, min_count: int = 3, asof: date | None = None, limit: int = 100) -> dict:
    """최근 months개월 동안 지각이 min_count회 이상인 직원 (많은 순). 조퇴는 기록이 없어 지각만 셈. [21]"""
    asof = _date(asof or date.today())
    start = _months_ago(asof, months)
    rows = db.fetch_all(
        f"SELECT e.emp_id, e.name, d.dept_name, e.grade, count(*) AS n "
        f"FROM hr.attendance a JOIN hr.employees e ON e.emp_id = a.emp_id {_DEPT} "
        f"WHERE a.status = '지각' AND a.work_date > %s AND a.work_date <= %s AND {_ACTIVE} "
        "GROUP BY e.emp_id, e.name, d.dept_name, e.grade", (start, asof))
    hit = sorted((r for r in rows if r["n"] >= min_count), key=lambda r: (-r["n"], r["emp_id"]))
    out = [{**_person(r), "지각": r["n"]} for r in hit]
    by_dept: dict[str, int] = {}
    for x in out:
        by_dept[x["부서"]] = by_dept.get(x["부서"], 0) + 1
    return {**_result({"기간": f"{start + timedelta(days=1)}~{asof}", "지각_이상": min_count}, out[:limit], len(out)),
            "부서별": by_dept, "주의": "출퇴근 기록에 조퇴 표시가 없어 지각만 셌어요. 교대근무자는 근무표가 없어 일근 기준이에요."}


def attendance_missing(month: str | None = None, limit: int = 100) -> dict:
    """그 달 출퇴근 기록 중 출근이나 퇴근 시각이 비어 있는 직원. month 예: '2026-10'. [22]"""
    if not month:
        last = _attendance_last_day() or date.today()
        month = f"{last.year}-{last.month:02d}"
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    rows = db.fetch_all(
        f"SELECT e.emp_id, e.name, d.dept_name, e.grade, count(*) AS n "
        f"FROM hr.attendance a JOIN hr.employees e ON e.emp_id = a.emp_id {_DEPT} "
        f"WHERE a.work_date >= %s AND a.work_date <= %s AND (a.check_in IS NULL OR a.check_out IS NULL) AND {_ACTIVE} "
        "GROUP BY e.emp_id, e.name, d.dept_name, e.grade ORDER BY n DESC, e.emp_id", (start, end))
    out = [{**_person(r), "누락일수": r["n"]} for r in rows]
    return {**_result({"월": month}, out[:limit], len(out)),
            "주의": "기록이 있는 날의 출근·퇴근 빈칸만 셌어요. 근무일인데 기록 자체가 없는 날은 근무표가 없어 못 셌어요."}


# ───────────── 연차 정산·발생 점검 ─────────────
_ORDINARY_HOURS = 209          # 통상임금 월 소정근로시간 (rules.json overtime.monthly_hours)
_DAY_HOURS = 8                 # 연차수당 1일 = 8시간분
_SICK_LIMIT_DAYS = 60          # 일반병가 연간 한도 (취업규칙 제25조)
_SICK_WORK_LIMIT_DAYS = 180    # 업무상 병가 연간 한도 (취업규칙 제25조)
_LONG_SERVICE_YEARS = 10       # 장기재직휴가 최소 근속 (취업규칙 제59조)


def _leave_pay(wage, days) -> int:
    """연차수당 = 통상임금 ÷ 209 × 8 × 잔여일수 (원 단위 반올림)"""
    return round(float(wage) / _ORDINARY_HOURS * _DAY_HOURS * float(days))


def _full_months(start: date, end: date) -> int:
    m = (end.year - start.year) * 12 + end.month - start.month
    return m - (1 if end.day < start.day else 0)


def _age(birth: date, asof: date) -> int:
    return asof.year - birth.year - ((asof.month, asof.day) < (birth.month, birth.day))


def _ids_result(cond: dict, rows: list[dict], limit: int | None = None, **extra) -> dict:
    """명단 결과: 표는 limit까지, 건수·ids는 전체"""
    shown = rows if limit is None else rows[:limit]
    return {**_result(cond, shown, len(rows)), "ids": [r["사번"] for r in rows], **extra}


def unused_leave_pay_estimate(year: int | None = None) -> dict:
    """올해 남은 연차를 수당으로 환산한 예상 총액 (직원별 가장 최근 급여월의 통상임금 기준). [6]"""
    year = year or _latest_year("leave_balance")
    rows = db.fetch_all(
        "SELECT lb.emp_id, lb.remaining_days, lb.asof_date, "
        "(SELECT p.ordinary_wage FROM hr.payroll p WHERE p.emp_id = lb.emp_id ORDER BY p.pay_month DESC LIMIT 1) AS wage "
        f"FROM hr.leave_balance lb JOIN hr.employees e ON e.emp_id = lb.emp_id WHERE lb.year = %s AND lb.remaining_days > 0 AND {_ACTIVE}",
        (year,))
    rows = [r for r in rows if r["wage"]]
    latest = db.fetch_one("SELECT max(pay_month) AS m FROM hr.payroll")["m"]
    asof = max((r["asof_date"] for r in rows if r["asof_date"]), default=None)
    return {**_result({"연도": year, "잔여일수 기준일": asof, "통상임금": f"{latest} 급여",
                       "식": "통상임금 ÷ 209 × 8 × 잔여일수"}, [], len(rows)),
            "총액": sum(_leave_pay(r["wage"], r["remaining_days"]) for r in rows),
            "주의": "직원별로 가장 최근 급여월의 통상임금을 썼어요. 급여 기록이 없는 직원은 뺐어요."}


def new_hire_monthly_leave(asof: date | None = None) -> dict:
    """입사 1년 미만 직원의 월 단위 연차(1개월 개근마다 1일, 최대 11일) 기대 발생일수와 실제 부여일수 비교. [9]"""
    asof = _date(asof or date.today())
    rows = db.fetch_all(
        "SELECT e.emp_id, e.name, d.dept_name, e.grade, e.hire_date, lb.granted_days "
        f"FROM hr.employees e {_DEPT} LEFT JOIN hr.leave_balance lb ON lb.emp_id = e.emp_id AND lb.year = %s "
        f"WHERE {_ACTIVE} AND e.hire_date > %s ORDER BY e.hire_date, e.emp_id",
        (asof.year, _months_ago(asof, 12)))
    out = []
    for r in rows:
        expected = min(max(_full_months(_date(r["hire_date"]), asof), 0), 11)
        out.append({**_person(r), "입사일": _d(r["hire_date"]), "기대발생일수": expected, "실제부여일수": _num(r["granted_days"])})
    out.sort(key=lambda x: x["사번"])
    mismatch = [x["사번"] for x in out if x["실제부여일수"] != x["기대발생일수"]]
    return _ids_result({"기준일": asof, "대상": "입사 1년 미만"}, out, 100, 불일치=len(mismatch))


def sick_leave_over_limit(year: int | None = None, limit_days: float = _SICK_LIMIT_DAYS,
                          work_related_limit_days: float = _SICK_WORK_LIMIT_DAYS) -> dict:
    """병가 연간 사용일수가 한도(일반 60일·업무상 180일)를 넘은 직원. 병명 등 민감정보는 안 줘요. [11]"""
    year = year or _latest_year("leave_balance")
    rows = db.fetch_all(
        "SELECT e.emp_id, e.name, d.dept_name, e.grade, lh.is_work_related, sum(lh.days) AS days "
        f"FROM hr.leave_history lh JOIN hr.employees e ON e.emp_id = lh.emp_id {_DEPT} "
        "WHERE lh.leave_type = '병가' AND lh.status = '승인' AND lh.start_date >= %s AND lh.start_date <= %s "
        "GROUP BY e.emp_id, e.name, d.dept_name, e.grade, lh.is_work_related", (date(year, 1, 1), date(year, 12, 31)))
    out = []
    for r in rows:
        work = r["is_work_related"] == "Y"
        cap = work_related_limit_days if work else limit_days
        if float(r["days"]) > cap:
            out.append({**_person(r), "구분": "업무상" if work else "일반", "병가일수": _num(r["days"]), "한도": cap})
    out.sort(key=lambda x: (-x["병가일수"], x["사번"]))
    return _ids_result({"연도": year, "일반병가한도": limit_days, "업무상병가한도": work_related_limit_days}, out, 100)


def long_service_leave_unused(year: int | None = None, min_years: int = _LONG_SERVICE_YEARS) -> dict:
    """장기재직휴가 대상(근속 min_years년 이상)인데 올해 아직 안 쓴 직원. 근속연수는 연도 차이로 셈. [12]"""
    year = year or _latest_year("leave_balance")
    rows = db.fetch_all(
        "SELECT e.emp_id, e.name, d.dept_name, e.grade, e.hire_date "
        f"FROM hr.employees e {_DEPT} JOIN hr.leave_balance lb ON lb.emp_id = e.emp_id AND lb.year = %s "
        f"WHERE {_ACTIVE} AND lb.long_service_leave_used <> 'Y' ORDER BY e.emp_id", (year,))
    out = [{**_person(r), "근속연수": year - _date(r["hire_date"]).year} for r in rows
           if year - _date(r["hire_date"]).year >= min_years]
    out.sort(key=lambda x: (-x["근속연수"], x["사번"]))
    return _ids_result({"연도": year, "근속이상": min_years}, out, 100)


def childcare_leave_eligible_unused(asof: date | None = None, child_max_age: int = 8, limit: int = 100) -> dict:
    """육아휴직을 쓸 수 있는데(만 child_max_age세 이하 자녀) 육아휴직 이력이 없는 직원. 자녀 이름은 안 줌. [35]"""
    asof = _date(asof or date.today())
    kids = db.fetch_all(
        "SELECT e.emp_id, e.name, d.dept_name, e.grade, f.family_id, f.birth_date "
        f"FROM hr.family f JOIN hr.employees e ON e.emp_id = f.emp_id {_DEPT} "
        f"WHERE f.relation = '자녀' AND f.deceased_date IS NULL AND {_ACTIVE} ORDER BY e.emp_id, f.birth_date")
    used = {r["emp_id"] for r in db.fetch_all("SELECT DISTINCT emp_id FROM hr.absence_history WHERE absence_type = '육아'")}
    people: dict[str, dict] = {}
    for r in kids:
        age = _age(_date(r["birth_date"]), asof)
        if age <= child_max_age and r["emp_id"] not in used:
            people.setdefault(r["emp_id"], {**_person(r), "자녀나이": []})["자녀나이"].append(age)
    out = list(people.values())
    return _ids_result({"기준일": asof, "자녀 만나이 이하": child_max_age}, out, limit)


def retiree_leave_payout(year: int | None = None) -> dict:
    """올해 퇴직자의 남은 연차 정산 금액 (퇴직월 통상임금 ÷ 209 × 8 × 잔여일수). [47]"""
    year = year or _latest_year("leave_balance")
    rows = db.fetch_all(
        "SELECT e.emp_id, e.name, d.dept_name, e.grade, e.retire_date, lb.remaining_days "
        f"FROM hr.employees e {_DEPT} JOIN hr.leave_balance lb ON lb.emp_id = e.emp_id AND lb.year = %s "
        "WHERE e.status = '퇴직' AND e.retire_date >= %s AND e.retire_date <= %s AND lb.remaining_days > 0 ORDER BY e.emp_id",
        (year, date(year, 1, 1), date(year, 12, 31)))
    out = []
    for r in rows:
        pm = _date(r["retire_date"]).strftime("%Y-%m")
        pay = db.fetch_one("SELECT ordinary_wage FROM hr.payroll WHERE emp_id = %s AND pay_month <= %s ORDER BY pay_month DESC LIMIT 1",
                           (r["emp_id"], pm))
        if not pay:
            continue
        out.append({**_person(r), "퇴직일": _d(r["retire_date"]), "잔여연차": _num(r["remaining_days"]),
                    "정산급여월": pm, "정산금액": _leave_pay(pay["ordinary_wage"], r["remaining_days"])})
    return {**_ids_result({"연도": year, "식": "퇴직월 통상임금 ÷ 209 × 8 × 잔여일수"}, out, 100),
            "총액": sum(x["정산금액"] for x in out)}


# ───────────── 퇴직·임금피크 ─────────────
_RETIRE_AGE = 60               # 정년 (rules.json company.retire_age)
_WAGE_PEAK_YEARS = 2           # 임금피크제 전환: 정년일 2년 전 (임금피크제 운영 내규 제4조)
_HONORARY_MIN_SERVICE = 20     # 명예퇴직 최소 근속 (인사규정 제48조)
_HONORARY_MIN_REMAIN = 1       # 명예퇴직 정년까지 최소 잔여 (인사규정 제48조)


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:             # 2/29
        return d.replace(year=d.year + years, day=28)


def _retire_date(birth: date, retire_age: int = _RETIRE_AGE) -> date:
    """정년퇴직일: 1~6월생 6/30, 7~12월생 12/31 (retirement_age_list와 같은 계산)"""
    y = birth.year + retire_age
    return date(y, 6, 30) if birth.month <= 6 else date(y, 12, 31)


def _active_people() -> list[dict]:
    return db.fetch_all(
        "SELECT e.emp_id, e.name, d.dept_name, e.grade, e.hire_date, e.birth_date "
        f"FROM hr.employees e {_DEPT} WHERE {_ACTIVE} ORDER BY e.emp_id")


def wage_peak_list(asof: date | None = None) -> dict:
    """임금피크제 적용 대상(정년일 2년 전부터 정년까지)과 몇 년차인지. 적용 여부 칸이 없어 생년월일로 계산. [39]"""
    asof = _date(asof or date.today())
    out = []
    for r in _active_people():
        retire = _retire_date(_date(r["birth_date"]))
        start = _add_years(retire, -_WAGE_PEAK_YEARS)
        if start <= asof <= retire:
            out.append({**_person(r), "전환기준일": _d(start), "년차": _age(start, asof) + 1, "정년일": _d(retire)})
    out.sort(key=lambda x: (x["정년일"], x["사번"]))
    return _ids_result({"기준일": asof, "전환 기준일": "정년 기준일 2년 전"}, out, 100)


def severance_estimate(retire_from: date | None = None, retire_to: date | None = None, limit: int = 100) -> dict:
    """정년퇴직 예정자(retire_from~retire_to)의 퇴직금 예상액 (평균임금 일액 × 30 × 근속연수). [41]"""
    start = _date(retire_from or date.today())
    end = _date(retire_to or date(start.year + 1, 12, 31))
    months = [r["pay_month"] for r in db.fetch_all(
        "SELECT DISTINCT pay_month FROM hr.payroll ORDER BY pay_month DESC LIMIT 3")]
    first, last = min(months), max(months)
    fy, fm = map(int, first.split("-"))
    ly, lm = map(int, last.split("-"))
    days = (date(ly + (lm == 12), lm % 12 + 1, 1) - date(fy, fm, 1)).days    # 최근 3개월 달력일수
    gross = {r["emp_id"]: float(r["g"]) for r in db.fetch_all(
        f"SELECT emp_id, sum(gross_pay) AS g FROM hr.payroll WHERE pay_month IN ({', '.join(['%s'] * len(months))}) GROUP BY emp_id",
        tuple(months))}
    out = []
    for r in _active_people():
        retire = _retire_date(_date(r["birth_date"]))
        if not start <= retire <= end or r["emp_id"] not in gross:
            continue
        years = ((retire.year - _date(r["hire_date"]).year) * 12 + retire.month - _date(r["hire_date"]).month) / 12
        daily = gross[r["emp_id"]] / days
        out.append({**_person(r), "퇴직일": _d(retire), "근속연수": round(years, 2), "평균임금일액": round(daily),
                    "예상퇴직금": round(daily * 30 * years)})
    out.sort(key=lambda x: (x["퇴직일"], x["사번"]))
    return _ids_result({"대상": "정년퇴직 예정자", "퇴직일": f"{start}~{end}", "식": "평균임금 일액 × 30 × 근속연수"}, out, limit,
                       주의=f"평균임금은 최근 3개월({first}~{last}) 총급여 ÷ {days}일 근사예요. 성과급·연차수당 1/4 가산은 반영하지 않았어요.")


def honorary_retirement_eligible(asof: date | None = None, min_service_years: int = _HONORARY_MIN_SERVICE,
                                 min_years_to_retire: int = _HONORARY_MIN_REMAIN) -> dict:
    """명예퇴직 신청 가능 직원(근속 min_service_years년 이상, 정년까지 min_years_to_retire년 이상). 인원수·사번만. [44]"""
    asof = _date(asof or date.today())
    ids = []
    for r in _active_people():
        service_ok = _age(_date(r["hire_date"]), asof) >= min_service_years
        remain_ok = _retire_date(_date(r["birth_date"])) >= _add_years(asof, min_years_to_retire)
        if service_ok and remain_ok:
            ids.append(r["emp_id"])
    return {**_result({"기준일": asof, "근속": f"{min_service_years}년 이상", "정년까지": f"{min_years_to_retire}년 이상"}, [], len(ids)),
            "ids": ids}


# ───────────── 출산 ─────────────
_BIRTH_BONUS = {1: 500000, 2: 2000000, 3: 3000000}   # 첫째·둘째·셋째 이상 (rules.json welfare_korail.birth_bonus, 2024년~)


def expected_births(asof: date | None = None) -> dict:
    """출산 예정인 직원 (승인된 출산전후휴가가 아직 안 끝난 직원). 휴가를 신청한 사람만 잡혀요. [50]"""
    asof = _date(asof or date.today())
    rows = db.fetch_all(
        "SELECT e.emp_id, e.name, d.dept_name, e.grade, lh.start_date, lh.end_date "
        f"FROM hr.leave_history lh JOIN hr.employees e ON e.emp_id = lh.emp_id {_DEPT} "
        "WHERE lh.leave_type = '출산전후' AND lh.status = '승인' AND lh.end_date >= %s ORDER BY lh.start_date, e.emp_id", (asof,))
    out = [{**_person(r), "휴가시작": _d(r["start_date"]), "휴가끝": _d(r["end_date"])} for r in rows]
    return _ids_result({"기준일": asof, "근거": "출산전후휴가 신청"}, out, 100, 주의="신청한 사람만 잡힘")


def birth_bonus_targets(month: str | None = None, limit: int = 100) -> dict:
    """그 달에 자녀가 태어나 가족으로 등록됐는데(출산 예정은 제외) 출산축하금을 아직 안 받은 직원과 예상 금액(첫째 50만·둘째 200만·셋째 이상 300만). month 예: '2026-11'. [53]"""
    if not month:
        month = date.today().strftime("%Y-%m")
    y, m = map(int, month.split("-"))
    start, end = date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    kids = db.fetch_all(
        "SELECT e.emp_id, e.name, d.dept_name, e.grade, f.family_id, f.birth_date, f.child_order "
        f"FROM hr.family f JOIN hr.employees e ON e.emp_id = f.emp_id {_DEPT} "
        f"WHERE f.relation = '자녀' AND f.registered_at IS NOT NULL AND f.birth_date >= %s AND f.birth_date <= %s AND {_ACTIVE} "
        "ORDER BY f.birth_date, e.emp_id",
        (start, end))
    paid = {r["emp_id"] for r in db.fetch_all(
        "SELECT emp_id FROM hr.requests WHERE request_type = '출산축하금신청' AND created_at >= %s", (start,))}
    out = [{**_person(r), "출생일": _d(r["birth_date"]), "출생순서": r["child_order"],
            "예상금액": _BIRTH_BONUS[min(int(r["child_order"] or 1), 3)]}
           for r in kids if r["emp_id"] not in paid]
    return _ids_result({"월": month}, out, limit)


# ───────────── 근태 (야간) ─────────────
_NIGHT_DAYS_PER_MONTH = 4      # 가정값: 월 평균 야간근로 4일 이상. 실제 기준은 산업안전보건법 시행규칙 확인 필요


def night_work_heavy(months: int = 6, min_days_per_month: float = _NIGHT_DAYS_PER_MONTH, asof: date | None = None,
                     limit: int = 100) -> dict:
    """최근 months개월 야간근로(22시 이후)가 잦은 직원: 특수건강진단 대상 후보 (월 평균 야간근로일 기준). [20]"""
    asof = _date(asof or _attendance_last_day() or date.today())
    start = _months_ago(asof, months)
    rows = db.fetch_all(
        "SELECT e.emp_id, e.name, d.dept_name, e.grade, count(*) AS n, sum(a.night_hours) AS h "
        f"FROM hr.attendance a JOIN hr.employees e ON e.emp_id = a.emp_id {_DEPT} "
        f"WHERE a.night_hours > 0 AND a.work_date > %s AND a.work_date <= %s AND {_ACTIVE} "
        "GROUP BY e.emp_id, e.name, d.dept_name, e.grade", (start, asof))
    hit = sorted((r for r in rows if r["n"] / months >= min_days_per_month), key=lambda r: (-r["n"], r["emp_id"]))
    out = [{**_person(r), "야간근로일수": r["n"], "야간근로시간": _num(r["h"]), "월평균일수": round(r["n"] / months, 1)} for r in hit]
    return _ids_result({"기간": f"{start + timedelta(days=1)}~{asof}", "월평균_이상": min_days_per_month}, out, limit,
                       주의="특수건강진단 야간작업 기준(시간·횟수)은 시행규칙 본문 확인이 필요해 월 평균 야간근로일로 근사했어요. "
                           "교대근무자는 근무표가 없어 일근 출퇴근 기록 기준이에요.")


# ───────────── 승진·전보 ─────────────
_PROMOTION_MIN_YEARS = {2: 4, 3: 4, 4: 4, 5: 3}   # 현 직급 최저 재직연수 (인사규정 제29조). 키는 현재 직급
_EXAM_FROM_GRADE, _EXAM_MIN_YEARS = 5, 3          # 4급 시험승진: 5급 3년 이상 (4급 시험승진 임용내규 제4조·별표1)
_LONG_STAY_YEARS = 5                              # 순환전보 대상 같은 부서 근속 (가정값, 인사규정 시행세칙 제20조에 기준 없음)


def _grade_since(r: dict) -> date:
    """현 직급 재직 시작일 = 마지막 승진일, 없으면 입사일 (휴직기간 차감은 안 함)"""
    return _date(r["last_promotion_date"] or r["hire_date"])


def _grade_people() -> list[dict]:
    return db.fetch_all(
        "SELECT e.emp_id, e.name, d.dept_name, e.grade, e.hire_date, e.last_promotion_date "
        f"FROM hr.employees e {_DEPT} WHERE {_ACTIVE} ORDER BY e.emp_id")


def promotion_min_years_met(year: int | None = None) -> dict:
    """올해 말 기준으로 승진 소요 최저연수(2~4급 4년, 5급 3년)를 채운 직원과 직급별 인원. [61]"""
    year = year or date.today().year
    asof = date(year, 12, 31)
    ids, by_grade = [], {}
    for r in _grade_people():
        need = _PROMOTION_MIN_YEARS.get(int(r["grade"]))
        if need and _age(_grade_since(r), asof) >= need:
            ids.append(r["emp_id"])
            by_grade[f"{r['grade']}급"] = by_grade.get(f"{r['grade']}급", 0) + 1
    return {**_result({"기준일": asof, "최저연수": {f"{g}급": y for g, y in _PROMOTION_MIN_YEARS.items()}}, [], len(ids)),
            "직급별": by_grade, "ids": ids, "주의": "휴직기간 차감은 반영 안 함 (현 직급 재직 = 마지막 승진일 또는 입사일부터)"}


def exam_promotion_4_candidates(asof: date | None = None, from_grade: int = _EXAM_FROM_GRADE,
                                min_years: int = _EXAM_MIN_YEARS) -> dict:
    """4급 시험승진 응시 대상(5급으로 3년 이상 재직). 승진임용예정인원은 DB에 없어 최저연수 경과자 전체. [65]"""
    asof = _date(asof or date.today())
    ids = [r["emp_id"] for r in _grade_people() if int(r["grade"]) == from_grade and _age(_grade_since(r), asof) >= min_years]
    return {**_result({"기준일": asof, "대상": f"{from_grade}급 {min_years}년 이상"}, [], len(ids)), "ids": ids,
            "주의": "실제 응시 범위는 승진임용예정인원의 2배수(본사)·1.2배수(지역) 이내인데 예정인원이 DB에 없음"}


def long_stay_same_dept(asof: date | None = None, min_years: int = _LONG_STAY_YEARS) -> dict:
    """같은 부서에 min_years년 이상 있는 직원 (순환전보 대상 후보). 마지막 전보일, 없으면 입사일부터 셈. [66]"""
    asof = _date(asof or date.today())
    last_move = {r["emp_id"]: r["d"] for r in db.fetch_all(
        "SELECT emp_id, max(effective_date) AS d FROM hr.hr_history WHERE event_type = '전보' GROUP BY emp_id")}
    ids = [r["emp_id"] for r in _grade_people()
           if _age(_date(last_move.get(r["emp_id"]) or r["hire_date"]), asof) >= min_years]
    return {**_result({"기준일": asof, "같은 부서": f"{min_years}년 이상(가정)"}, [], len(ids)), "ids": ids,
            "주의": "'오래'의 기준이 규정에 없어 같은 부서 5년 이상으로 가정했어요."}


def transfer_restricted(asof: date | None = None) -> dict:
    """전보제한 기간 때문에 못 옮기는 직원. 채용 구분(전국권·지역권·경력)이 DB에 없어 계산할 수 없어요. [67]"""
    asof = _date(asof or date.today())
    return {**_result({"기준일": asof}, [], 0), "ids": [], "계산불가": True,
            "주의": "전보제한 기간은 채용 구분(전국권·지역권·경력)에 따라 달라지는데(직원 전보제한 운영 내규 제5조·별표2) 그 구분이 DB에 없어 "
                    "대상자를 계산할 수 없어요. DB 담당자에게 채용 구분 칸을 요청해야 해요."}


# ───────────── 교육시간 ─────────────
def training_hours_lowest(year: int | None = None, limit: int = 10) -> dict:
    """올해 이수한 교육시간 합계가 가장 적은 재직자 (적은 순). [83]"""
    year = year or _latest_year("training")
    rows = db.fetch_all(
        "SELECT e.emp_id, e.name, d.dept_name, e.grade, "
        "COALESCE(sum(CASE WHEN t.status = '이수' THEN t.hours ELSE 0 END), 0) AS h "
        f"FROM hr.employees e {_DEPT} LEFT JOIN hr.training t ON t.emp_id = e.emp_id AND t.year = %s "
        f"WHERE {_ACTIVE} GROUP BY e.emp_id, e.name, d.dept_name, e.grade ORDER BY h, e.emp_id LIMIT %s", (year, limit))
    out = [{**_person(r), "교육시간": _num(r["h"])} for r in rows]
    return _ids_result({"연도": year, "기준": "이수한 교육시간 합계"}, out, None, 최소시간=out[0]["교육시간"] if out else None)


# ───────────── 수당·복지 ─────────────
_CHILD_AGE_LIMIT = 19          # 가족수당 자녀 기준: 19세 미만 (보수규정 시행세칙 제24조, rules.json family_allowance)
_LONG_SERVICE_BRACKETS = (5, 10, 15, 20)   # 장기근속수당 구간 시작 연수 (rules.json long_service_allowance)
_LONG_SERVICE_MAX_GRADE_NUM = 3            # "3급 이하"만 지급 (보수규정 시행세칙 제23조): 급 숫자가 3 이상
_WELFARE_BASE = 800000         # 복지포인트 연 배정 (rules.json welfare_points.base)


def _latest_pay(emp_ids=None) -> dict:
    latest = db.fetch_one("SELECT max(pay_month) AS m FROM hr.payroll")["m"]
    return {r["emp_id"]: r for r in db.fetch_all(
        "SELECT emp_id, family_allowance FROM hr.payroll WHERE pay_month = %s", (latest,))}, latest


def family_allowance_overage(asof: date | None = None, child_age_limit: int = _CHILD_AGE_LIMIT) -> dict:
    """가족수당을 받는데 자녀가 기준 나이(19세 미만)를 넘은 직원. 자녀 이름은 안 줌. [86]"""
    asof = _date(asof or date.today())
    pay, _ = _latest_pay()
    kids = db.fetch_all(
        "SELECT e.emp_id, e.name, d.dept_name, e.grade, f.birth_date FROM hr.family f "
        f"JOIN hr.employees e ON e.emp_id = f.emp_id {_DEPT} "
        f"WHERE f.relation = '자녀' AND f.is_dependent = 'Y' AND f.deceased_date IS NULL AND {_ACTIVE} ORDER BY e.emp_id, f.birth_date")
    people: dict[str, dict] = {}
    for r in kids:
        age = _age(_date(r["birth_date"]), asof)
        fa = int((pay.get(r["emp_id"]) or {}).get("family_allowance") or 0)
        if age >= child_age_limit and fa > 0:
            people.setdefault(r["emp_id"], {**_person(r), "자녀나이": [], "가족수당": fa})["자녀나이"].append(age)
    return _ids_result({"기준일": asof, "자녀 기준": f"{child_age_limit}세 미만"}, list(people.values()), 100)


def long_service_bracket_change(month: str | None = None) -> dict:
    """그 달에 근속 5·10·15·20년이 돼 장기근속수당 구간이 바뀌는 3급 이하 직원. month 예: '2026-12'. [87]"""
    if not month:
        t = date.today()
        month = f"{t.year + (t.month == 12)}-{t.month % 12 + 1:02d}"
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    out = []
    for r in _active_people():
        if int(r["grade"]) < _LONG_SERVICE_MAX_GRADE_NUM:
            continue
        h = _date(r["hire_date"])
        for yrs in _LONG_SERVICE_BRACKETS:
            if start <= _add_years(h, yrs) <= end:
                out.append({**_person(r), "입사일": _d(h), "새구간": f"{yrs}년 이상"})
    out.sort(key=lambda x: (x["입사일"], x["사번"]))
    return {**_ids_result({"기준일": start, "대상": "3급 이하"}, out, 100), "주의": "근속은 입사일 기준(군복무·공무원 경력 합산 미반영)"}


def couple_family_allowance(pay_month: str | None = None) -> dict:
    """부부가 둘 다 직원인데 가족수당(배우자분)을 둘 다 받는 경우. 배우자 이름+생년월일로 맞춤(사번 칸이 없음). [109]"""
    pay_month = pay_month or db.fetch_one("SELECT max(pay_month) AS m FROM hr.payroll")["m"]
    emp = {(r["name"], str(r["birth_date"])): r for r in _active_people_full()}
    fa = {r["emp_id"]: int(r["family_allowance"] or 0) for r in db.fetch_all(
        "SELECT emp_id, family_allowance FROM hr.payroll WHERE pay_month = %s", (pay_month,))}
    spouses = db.fetch_all("SELECT emp_id, name, birth_date FROM hr.family WHERE relation = '배우자' AND is_dependent = 'Y'")
    seen, out = set(), []
    for sp in spouses:
        other = emp.get((sp["name"], str(sp["birth_date"])))
        if not other or other["emp_id"] == sp["emp_id"] or not (fa.get(sp["emp_id"]) and fa.get(other["emp_id"])):
            continue
        pair = tuple(sorted((sp["emp_id"], other["emp_id"])))
        if pair in seen:
            continue
        seen.add(pair)
        for eid in pair:
            out.append({**_person(next(x for x in emp.values() if x["emp_id"] == eid)), "가족수당": fa[eid]})
    return _ids_result({"급여월": pay_month, "매칭": "배우자 이름+생년월일"}, out, 100)


def _active_people_full() -> list[dict]:
    return db.fetch_all(
        "SELECT e.emp_id, e.name, d.dept_name, e.grade, e.birth_date "
        f"FROM hr.employees e {_DEPT} WHERE {_ACTIVE}")


def welfare_points_proration_check(year: int | None = None) -> dict:
    """올해 중간에 입사·복직한 직원의 복지포인트가 월할(사유 발생 달부터 12월까지)로 맞게 부여됐는지. [111]"""
    year = year or _latest_year("welfare_points")
    first, last = date(year, 1, 1), date(year, 12, 31)
    reasons: dict[str, date] = {}
    for r in db.fetch_all("SELECT emp_id, hire_date AS d FROM hr.employees WHERE hire_date >= %s AND hire_date <= %s", (first, last)):
        reasons[r["emp_id"]] = _date(r["d"])
    for r in db.fetch_all("SELECT emp_id, return_date AS d FROM hr.absence_history WHERE return_date >= %s AND return_date <= %s", (first, last)):
        reasons[r["emp_id"]] = max(reasons.get(r["emp_id"], first), _date(r["d"]))
    rows = db.fetch_all(
        "SELECT e.emp_id, e.name, d.dept_name, e.grade, w.granted_points "
        f"FROM hr.welfare_points w JOIN hr.employees e ON e.emp_id = w.emp_id {_DEPT} WHERE w.year = %s AND {_ACTIVE} ORDER BY e.emp_id", (year,))
    out = []
    for r in rows:
        if r["emp_id"] in reasons:
            when = reasons[r["emp_id"]]
            expected = round(_WELFARE_BASE * (13 - when.month) / 12)
            out.append({**_person(r), "사유발생일": _d(when), "기대포인트": expected, "부여포인트": int(r["granted_points"])})
    wrong = [x["사번"] for x in out if abs(x["기대포인트"] - x["부여포인트"]) > 1000]
    return {**_ids_result({"연도": year, "식": "사유 발생 달부터 12월까지 별표 1 월 배정 합계"}, out, 100), "불일치_ids": wrong,
            "주의": "휴직 기간 차감 방식은 규정에 없어 복직자는 복직 달부터 계산. 1,000포인트 이내 차이는 반올림이라 정상"}


def service_anniversary(year: int | None = None, years: list[int] | None = None) -> dict:
    """올해 근속 10·20·30년이 되는 직원 (근속연수는 연도 차이로 셈). years로 다른 연수도 가능. [97]"""
    year = year or date.today().year
    years = [int(y) for y in (years or [10, 20, 30])]
    out = []
    for r in _active_people():
        n = year - _date(r["hire_date"]).year
        if n in years:
            out.append({**_person(r), "입사일": _d(r["hire_date"]), "근속": n})
    out.sort(key=lambda x: (-x["근속"], x["입사일"], x["사번"]))
    return _ids_result({"연도": year}, out, 100)


# ───────────── 출장·경조금 ─────────────
# 여비및시험수당지급세칙 별표 2 (국내여비 정액표): 일비·식비는 2급 이상 각 3만원, 3급 이하 각 2.5만원 (1일당),
# 숙박비 상한은 3급 이하 서울 10만·광역시 8만·그 밖 7만 (2급 이상은 '실비'라 같은 상한으로 근사)
_TRIP_PER_DIEM = {"high": 30000, "low": 25000}     # high: 2급 이상, low: 3급 이하 (일비 = 식비)
_LODGING_CAP = {"서울": 100000, "광역시": 80000, "그 밖": 70000}
_LONG_TRIP_CUTS = ((15, 30, 0.1), (30, None, 0.2))  # 박수 기준 15박 초과 10%, 30박 초과 20% 일비 감액 (rules.json business_trip)


def _per_diem(grade) -> int:
    return _TRIP_PER_DIEM["high"] if int(grade) <= 2 else _TRIP_PER_DIEM["low"]


def trips_by_dept(month: str | None = None, limit: int = 50) -> dict:
    """그 달에 출발하는 출장을 부서별로 건수와 예상 출장비(일수×(일비+식비) + 박수×숙박비 상한)로. 운임은 실비라 제외. [99]"""
    if not month:
        month = date.today().strftime("%Y-%m")
    y, m = map(int, month.split("-"))
    start, end = date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    rows = db.fetch_all(
        "SELECT d.dept_name, e.grade, t.region_type, t.start_date, t.end_date, t.nights "
        f"FROM hr.business_trips t JOIN hr.employees e ON e.emp_id = t.emp_id {_DEPT} "
        "WHERE t.start_date >= %s AND t.start_date <= %s ORDER BY t.trip_id", (start, end))
    by_dept: dict[str, dict] = {}
    for r in rows:
        days = (_date(r["end_date"]) - _date(r["start_date"])).days + 1
        cost = days * 2 * _per_diem(r["grade"]) + int(r["nights"] or 0) * _LODGING_CAP.get(r["region_type"], 70000)
        x = by_dept.setdefault(r["dept_name"], {"부서": r["dept_name"], "출장건수": 0, "예상출장비": 0})
        x["출장건수"] += 1
        x["예상출장비"] += cost
    out = sorted(by_dept.values(), key=lambda x: -x["출장건수"])   # 같은 건수면 먼저 출장 간 부서 순
    return {**_result({"월": month, "식": "일수×(일비+식비) + 박수×숙박 상한"}, out[:limit], len(out)),
            "주의": "운임(철도·버스)은 실비라 제외"}


def trips_unsettled(asof: date | None = None, limit: int = 100) -> dict:
    """출장이 끝났는데(종료일이 asof 전) 상태가 '완료'가 아닌 출장 (정산 전). 정산 기한은 규정 확인 필요. [112]"""
    asof = _date(asof or date.today())
    rows = db.fetch_all(
        "SELECT e.emp_id, e.name, d.dept_name, e.grade, t.trip_id, t.end_date, t.status "
        f"FROM hr.business_trips t JOIN hr.employees e ON e.emp_id = t.emp_id {_DEPT} "
        "WHERE t.end_date < %s AND t.status <> '완료' ORDER BY t.end_date, t.trip_id", (asof,))
    out = [{**_person(r), "출장번호": r["trip_id"], "종료일": _d(r["end_date"]), "상태": r["status"]} for r in rows]
    return _ids_result({"기준일": asof, "미정산": "상태가 '완료'가 아닌 건"}, out, limit,
                       주의="정산 기한은 규정(여비및시험수당지급세칙 제4조)을 확인해야 해요. 여기선 종료일이 지난 건을 모두 셌어요.")


def long_trip_per_diem_check(min_days: int = 15, limit: int = 100) -> dict:
    """같은 지역 장기 출장(박수가 min_days 초과)의 일비 감액 예정액 (15박 초과 10%, 30박 초과 20%). [113]"""
    rows = db.fetch_all(
        "SELECT e.emp_id, e.name, d.dept_name, e.grade, t.trip_id, t.region_type, t.start_date, t.end_date, t.nights "
        f"FROM hr.business_trips t JOIN hr.employees e ON e.emp_id = t.emp_id {_DEPT} "
        "WHERE t.nights > %s ORDER BY t.trip_id", (min_days,))
    out = []
    for r in rows:
        n = int(r["nights"])
        cut = 0.0
        for lo, hi, rate in _LONG_TRIP_CUTS:
            cut += max(0, min(n, hi if hi else n) - lo) * rate
        out.append({**_person(r), "출장번호": r["trip_id"], "일수": (_date(r["end_date"]) - _date(r["start_date"])).days + 1,
                    "지역": r["region_type"], "일비감액": round(cut * _per_diem(r["grade"]))})
    return {**_ids_result({"기준": f"같은 지역 {min_days}일 초과 출장"}, out, limit),
            "주의": "실제 지급 일비 데이터가 없어 감액 예정액만 계산"}


def duplicate_condolence_payments(limit: int = 100) -> dict:
    """같은 직원·같은 가족·같은 경조 종류로 경조금이 두 번 이상 나간 건. [110]"""
    rows = db.fetch_all(
        "SELECT r.request_id, r.event_type, r.payload, e.emp_id, e.name, d.dept_name, e.grade "
        f"FROM hr.requests r JOIN hr.employees e ON e.emp_id = r.emp_id {_DEPT} "
        "WHERE r.request_type = '경조금신청' AND r.status = '지급완료' ORDER BY r.request_id")
    groups: dict[tuple, list] = {}
    for r in rows:
        try:
            fam = json.loads(r["payload"] or "{}").get("family_id")
        except ValueError:
            fam = None
        groups.setdefault((r["emp_id"], fam, r["event_type"]), []).append(r)
    out = [{**_person(g[0]), "경조종류": g[0]["event_type"], "지급횟수": len(g), "신청번호": [x["request_id"] for x in g]}
           for g in groups.values() if len(g) > 1]
    out.sort(key=lambda x: x["사번"])
    return _ids_result({"기준": "같은 직원·같은 가족·같은 경조 종류"}, out, limit)


# ───────────── 로그 ─────────────
def audit_log_recent(action: str | None = None, limit: int = 50) -> dict:
    """최근 감사로그(누가 언제 어떤 직원 정보를 조회·변경했는지). action 예: '개인정보 조회'. 기록이 없으면 없다고 답함. [116]"""
    sql = ("SELECT a.log_id, a.actor_user_id, a.action, a.target_emp_id, a.created_at FROM hr.audit_log a")
    params: tuple = ()
    if action:
        sql += " WHERE a.action = %s"
        params = (action,)
    rows = db.fetch_all(sql + " ORDER BY a.created_at DESC, a.log_id DESC LIMIT %s", params + (limit,))
    out = [{"로그번호": r["log_id"], "행위자": r["actor_user_id"], "행위": r["action"], "대상사번": r["target_emp_id"],
            "일시": _d(r["created_at"])} for r in rows]
    result = _result({"테이블": "audit_log", "행위": action}, out)
    if not out:
        result["주의"] = "audit_log에 기록이 없어요. 조회 기록이 없다는 뜻이에요."
    return result


def chat_topic_stats(month: str | None = None, limit: int = 5) -> dict:
    """직원들이 챗봇에 많이 물어본 주제(의도·종류)별 질문 수 TOP N. 질문 내용·질문한 사람은 안 줌 (주제 집계만). [L01, L02]"""
    sql = "SELECT intent, kind, count(*) AS n FROM app.chat_log"
    params: tuple = ()
    cond: dict = {"상위": limit}
    if month:
        y, m = map(int, month.split("-"))
        start, end = date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1)
        sql += " WHERE created_at >= %s AND created_at < %s"
        params = (start, end)
        cond["월"] = month
    rows = db.fetch_all(sql + " GROUP BY intent, kind ORDER BY n DESC, intent, kind LIMIT %s", params + (limit,))
    out = [{"주제": r["intent"] or "미분류", "종류": r["kind"] or "-", "질문수": r["n"]} for r in rows]
    return {**_result(cond, out), "주의": "질문 내용과 질문한 사람은 보여주지 않고 주제별 건수만 집계해요."}


# ───────────── 연차 사용 촉진 (근로기준법 제61조) ─────────────
def _promotion_stage(asof: date) -> dict:
    """연차 기간은 1/1~12/31 (취업규칙 제23조⑤). 소멸 6개월 전 10일 안에 1차, 소멸 2개월 전까지 2차 통보."""
    y = asof.year
    first_from, first_to, second_to = date(y, 7, 1), date(y, 7, 10), date(y, 10, 31)
    if asof < first_from:
        stage = "1차 통보 전"
    elif asof <= first_to:
        stage = "1차 통보 기간"
    elif asof <= second_to:
        stage = "2차 통보 기간"
    else:
        stage = "통보 기한 지남"
    return {"현재 단계": stage, "연차 소멸일": _d(date(y, 12, 31)),
            "1차 통보": f"{first_from}~{first_to} (미사용 일수를 알리고 사용 시기를 정해 내도록 서면 촉구)",
            "2차 통보": f"~{second_to} (직원이 10일 안에 시기를 안 내면 회사가 사용 시기를 정해 서면 통보)"}


def leave_promotion_targets(asof: date | None = None, min_days: float = 1, limit: int = 20) -> dict:
    """연차 사용 촉진 대상(올해 남은 연차가 min_days일 이상인 1년 이상 재직자)과 부서별 촉진 필요 여부. 통보 단계·기한 포함."""
    asof = _date(asof or date.today())
    year = asof.year
    one_year_ago = _months_ago(asof, 12)
    rows = db.fetch_all(
        "SELECT e.emp_id, e.name, d.dept_name, e.grade, e.hire_date, lb.remaining_days, lb.asof_date "
        f"FROM hr.leave_balance lb JOIN hr.employees e ON e.emp_id = lb.emp_id {_DEPT} "
        f"WHERE lb.year = %s AND {_ACTIVE}", (year,))
    dept: dict[str, dict] = {}
    targets, new_hires = [], 0
    for r in rows:
        x = dept.setdefault(r["dept_name"], {"부서": r["dept_name"], "인원": 0, "대상자": 0, "잔여합계": 0.0})
        x["인원"] += 1
        if _date(r["hire_date"]) > one_year_ago:      # 1년 미만 입사자는 촉진 시기가 달라(제61조②) 따로 셈
            new_hires += 1
            continue
        remaining = float(r["remaining_days"] or 0)
        if remaining >= min_days:
            x["대상자"] += 1
            x["잔여합계"] += remaining
            targets.append(r)
    table = []
    for x in dept.values():
        table.append({**x, "대상비율": round(x["대상자"] / x["인원"], 3) if x["인원"] else 0.0,
                      "잔여합계": round(x["잔여합계"], 1), "촉진필요": "예" if x["대상자"] else "아니요"})
    table.sort(key=lambda x: (-x["대상자"], x["부서"]))
    targets.sort(key=lambda r: (-float(r["remaining_days"] or 0), r["emp_id"]))
    data_asof = max((_date(r["asof_date"]) for r in rows if r["asof_date"]), default=None)
    return {**_result({"기준일": asof, "연도": year, "잔여일수_이상": min_days, "잔여 기준일": data_asof}, table, len(targets)),
            "명단": [{**_person(r), "잔여일수": _num(r["remaining_days"])} for r in targets[:limit]],
            "단계": _promotion_stage(asof),
            "주의": f"1년 미만 입사자 {new_hires}명은 촉진 시기가 달라(근로기준법 제61조②) 대상에서 뺐어요. "
                    "통보를 실제로 했는지는 DB에 기록이 없어요."}


# ───────────── 등록표 · 실행 ─────────────
# 이름 → 함수. 의도 분류(LLM)가 이 중 하나와 인자를 고르면 run_admin_tool이 실행해요.
ADMIN_TOOLS = {f.__name__: f for f in (
    dept_leave_usage, leave_remaining_list, leave_promotion_targets, leave_by_dept_period, leave_people_period,
    unused_leave_pay_estimate, new_hire_monthly_leave, sick_leave_over_limit, long_service_leave_unused,
    childcare_leave_eligible_unused, retiree_leave_payout,
    wage_peak_list, severance_estimate, honorary_retirement_eligible, expected_births, birth_bonus_targets, night_work_heavy,
    promotion_min_years_met, exam_promotion_4_candidates, long_stay_same_dept, transfer_restricted, training_hours_lowest,
    family_allowance_overage, long_service_bracket_change, couple_family_allowance, welfare_points_proration_check,
    service_anniversary,
    trips_by_dept, trips_unsettled, long_trip_per_diem_check, duplicate_condolence_payments, audit_log_recent, chat_topic_stats,
    pending_requests, weekly_hours_over, late_early_frequent, attendance_missing,
    absence_list, retirement_age_list, retirements_by_dept,
    probation_ending, hires_in_period, hr_events,
    training_incomplete, dept_training_rate,
    payroll_dept_totals, net_pay_change,
    welfare_points_dept_usage, welfare_points_expiring,
)}


def tool_catalog() -> list[dict]:
    """LLM 프롬프트에 넣을 도구 목록: 이름, 설명(첫 줄), 인자 이름"""
    return [{"name": n, "description": (f.__doc__ or "").strip().splitlines()[0],
             "params": list(inspect.signature(f).parameters)} for n, f in ADMIN_TOOLS.items()]


def run_admin_tool(role: str, name: str, params: dict | None = None) -> dict:
    """역할 확인 → 도구 이름 확인 → 모르는 인자 제거 → 실행. role은 반드시 토큰에서 온 값."""
    if role != "hr_admin":
        raise PermissionError("인사팀(hr_admin) 전용 조회예요.")
    fn = ADMIN_TOOLS.get(name)
    if fn is None:
        raise KeyError(f"모르는 도구 {name!r}")
    allowed = inspect.signature(fn).parameters
    return fn(**{k: v for k, v in (params or {}).items() if k in allowed and v is not None})
