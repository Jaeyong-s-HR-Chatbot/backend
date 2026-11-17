"""인사팀(hr_admin) 전용 집계·명단 조회 도구. 정해진 SQL만 돌려요 (LLM이 SQL을 쓰지 않음).

- 호출은 run_admin_tool()로만 해요. 역할(role)은 로그인 토큰에서 온 값이어야 하고, hr_admin이 아니면 막아요.
- 주민번호·계좌·주소(hr.employee_pii)와 가족 이름은 읽지 않아요. 명단에는 사번·이름·부서·직급까지만 넣어요.
- 생리·보건휴가처럼 민감한 휴가는 명단 없이 인원수만 돌려줘요.
- 규정 기준(정년 나이, 기준 비율 등)은 인자로 받아요. 값은 규정 검색 결과나 rules.json에서 정해 넘겨요.
- 결과는 {"기준": 조회 조건, "건수": n, "표": [...]} 모양이고, 명단이 길면 limit까지만 담고 "건수"에 전체 수를 적어요.
- 관리자 질문셋(질문셋/관리자_질문셋_v2.csv)의 데이터 질문 번호를 함수마다 적어 뒀어요.
"""
import inspect
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


def leave_by_dept_period(start: date, end: date, leave_type: str | None = None) -> dict:
    """기간 안에 휴가 쓰는(쓴) 인원을 부서별로. 휴가자 몰린 부서, 공가·생리휴가 사용 현황. 인원수만 돌려줌. [10, 14, 30]"""
    start, end = _date(start), _date(end)
    sql = (f"SELECT d.dept_name, count(DISTINCT lh.emp_id) AS n, sum(lh.days) AS days "
           f"FROM hr.leave_history lh JOIN hr.employees e ON e.emp_id = lh.emp_id {_DEPT} "
           "WHERE lh.status = '승인' AND lh.start_date <= %s AND lh.end_date >= %s")
    params: tuple = (end, start)
    if leave_type:
        sql += " AND lh.leave_type = %s"
        params += (leave_type,)
    rows = db.fetch_all(sql + " GROUP BY d.dept_name ORDER BY n DESC, d.dept_name", params)
    out = [{"부서": r["dept_name"], "인원": r["n"], "휴가일수": _num(r["days"])} for r in rows]
    return _result({"시작": start, "끝": end, "휴가종류": leave_type}, out)


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
def training_incomplete(year: int | None = None, course_type: str = "법정", limit: int = 100) -> dict:
    """필수 교육 미이수자 명단 (과정별). course_type: 법정 / 직무 / 승진 / 신규 / 리더십. [74, 82]"""
    year = year or _latest_year("training")
    where = (f"FROM hr.training t JOIN hr.employees e ON e.emp_id = t.emp_id {_DEPT} "
             f"WHERE t.year = %s AND t.course_type = %s AND t.required = 'Y' AND t.status <> '이수' AND {_ACTIVE}")
    total = db.fetch_one(f"SELECT count(*) AS n {where}", (year, course_type))["n"]
    rows = db.fetch_all(f"SELECT e.emp_id, e.name, d.dept_name, e.grade, t.course_name, t.status {where} "
                        "ORDER BY t.course_name, d.dept_name, e.emp_id LIMIT %s", (year, course_type, limit))
    out = [{**_person(r), "과정": r["course_name"], "상태": r["status"]} for r in rows]
    return _result({"연도": year, "교육구분": course_type}, out, total)


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


# ───────────── 등록표 · 실행 ─────────────
# 이름 → 함수. 의도 분류(LLM)가 이 중 하나와 인자를 고르면 run_admin_tool이 실행해요.
ADMIN_TOOLS = {f.__name__: f for f in (
    dept_leave_usage, leave_remaining_list, leave_by_dept_period, leave_people_period, pending_requests,
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
