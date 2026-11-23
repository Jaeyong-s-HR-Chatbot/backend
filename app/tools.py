"""본인 조회 도구. 정해진 SQL과 정해진 컬럼만 돌려줘요 (LLM이 SQL을 쓰지 않음).

- emp_id는 반드시 로그인 토큰에서 온 값만 넣어요.
- 주민번호·계좌·주소(hr.employee_pii)와 가족 이름은 읽지 않아요.
- 금액·일수는 DB 값 그대로 돌려주고, 기준일(asof)을 함께 붙여요.
"""
from datetime import date

from . import db


def _d(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def get_profile(emp_id: str) -> dict:
    r = db.fetch_one(
        "SELECT e.name, d.dept_name, e.position, e.grade, e.duty, e.job_family, e.work_pattern, e.site, e.status, "
        "e.hire_date, e.birth_date FROM hr.employees e LEFT JOIN hr.departments d USING (dept_id) WHERE e.emp_id = %s", (emp_id,))
    if not r:
        return {}
    today = date.today()
    years = today.year - r["hire_date"].year - ((today.month, today.day) < (r["hire_date"].month, r["hire_date"].day))
    age = today.year - r["birth_date"].year - ((today.month, today.day) < (r["birth_date"].month, r["birth_date"].day))
    return {"부서": r["dept_name"], "직위": r["position"], "직급": f"{r['grade']}급", "직책": r["duty"], "직군": r["job_family"],
            "근무형태": r["work_pattern"], "근무지": r["site"], "재직상태": r["status"], "입사일": _d(r["hire_date"]),
            "근속연수": years, "나이": age}


def get_leave_balance(emp_id: str) -> dict:
    r = db.fetch_one("SELECT year, granted_days, carried_days, used_days, remaining_days, asof_date FROM hr.leave_balance "
                     "WHERE emp_id = %s ORDER BY year DESC LIMIT 1", (emp_id,))
    if not r:
        return {}
    return {"연도": r["year"], "부여일수": r["granted_days"], "이월일수": r["carried_days"], "사용일수": float(r["used_days"]),
            "잔여일수": float(r["remaining_days"]), "기준일": _d(r["asof_date"])}


def get_welfare_points(emp_id: str) -> dict:
    r = db.fetch_one("SELECT year, granted_points, used_points, expires_on FROM hr.welfare_points WHERE emp_id = %s "
                     "ORDER BY year DESC LIMIT 1", (emp_id,))
    if not r:
        return {}
    return {"연도": r["year"], "부여포인트": r["granted_points"], "사용포인트": r["used_points"],
            "잔여포인트": r["granted_points"] - r["used_points"], "만료일": _d(r["expires_on"])}


def get_family(emp_id: str) -> dict:
    rows = db.fetch_all("SELECT relation, birth_date, is_dependent, child_order FROM hr.family WHERE emp_id = %s "
                        "AND deceased_date IS NULL ORDER BY relation, birth_date", (emp_id,))
    today = date.today()
    fam = [{"관계": r["relation"], "나이": today.year - r["birth_date"].year - ((today.month, today.day) < (r["birth_date"].month, r["birth_date"].day)),
            "부양가족": r["is_dependent"] == "Y"} for r in rows]
    return {"가족": fam, "부양가족수": sum(f["부양가족"] for f in fam)} if fam else {}


def get_payroll(emp_id: str) -> dict:
    r = db.fetch_one("SELECT pay_month, ordinary_wage, family_allowance, long_service_allowance, shift_allowance, overtime_hours, "
                     "overtime_pay, night_hours, night_pay, holiday_hours, holiday_pay, gross_pay, deductions, net_pay, pay_date "
                     "FROM hr.payroll WHERE emp_id = %s ORDER BY pay_month DESC LIMIT 1", (emp_id,))
    if not r:
        return {}
    return {"급여월": r["pay_month"], "통상임금": r["ordinary_wage"], "가족수당": r["family_allowance"], "장기근속수당": r["long_service_allowance"],
            "교대수당": r["shift_allowance"], "시간외근무시간": float(r["overtime_hours"]), "시간외수당": r["overtime_pay"],
            "야간근무시간": float(r["night_hours"]), "야간수당": r["night_pay"], "휴일근무시간": float(r["holiday_hours"]), "휴일수당": r["holiday_pay"],
            "지급총액": r["gross_pay"], "공제액": r["deductions"], "실수령액": r["net_pay"], "지급일": _d(r["pay_date"])}


def get_requests(emp_id: str, limit: int = 5) -> dict:
    rows = db.fetch_all("SELECT request_id, request_type, event_type, status, created_at, decided_at FROM hr.requests "
                        "WHERE emp_id = %s ORDER BY created_at DESC, request_id DESC LIMIT %s", (emp_id, limit))
    return {"최근신청": [{"번호": r["request_id"], "종류": r["request_type"], "내용": r["event_type"], "상태": r["status"],
                       "신청일": _d(r["created_at"]), "결재일": _d(r["decided_at"])} for r in rows]} if rows else {}


def get_training(emp_id: str) -> dict:
    rows = db.fetch_all("SELECT course_name, required, status, completed_on FROM hr.training WHERE emp_id = %s AND year = "
                        "(SELECT max(year) FROM hr.training WHERE emp_id = %s) ORDER BY required DESC, course_name", (emp_id, emp_id))
    if not rows:
        return {}
    todo = [r["course_name"] for r in rows if r["required"] == "Y" and r["status"] != "이수"]
    return {"필수교육_미이수": todo, "이수과정수": sum(r["status"] == "이수" for r in rows), "전체과정수": len(rows)}


_MARRIAGE_BENEFIT = ("결혼축하금", "결혼하는데", "결혼 예정")

# 질문에 이 단어가 있으면 이 도구를 써요 (코드가 고름, LLM이 고르지 않음)
TOOL_KEYWORDS = [
    (("연차", "휴가 며칠 남", "휴가 몇"), "leave_balance", get_leave_balance),
    (("복지포인트", "복지 포인트", "포인트"), "welfare_points", get_welfare_points),
    # 가족수당 금액은 급여(payroll)에 있어요. family는 부양가족 등록 자료(관계·인정 여부)만
    (("급여", "월급", "수당 얼마 나왔", "시간외수당", "야근수당", "실수령", "통상임금", "수당 내역", "수당 금액"),
     "payroll", get_payroll),
    # 결혼축하금은 생애 1회 한정 (팀 결정 2026-11-18): 본인 결혼 혜택 질문은 배우자 여부(family)와
    # 과거 경조금 신청(requests)을 같이 봐요
    (("부양가족", "가족 등록", *_MARRIAGE_BENEFIT), "family", get_family),
    (("신청", "결재", "서류", *_MARRIAGE_BENEFIT), "requests", get_requests),
    (("교육", "법정교육", "이수"), "training", get_training),
]
# 이 표현은 도구 단어를 포함해도 해당 도구로 보지 않아요 ("부양가족 수당"은 급여 조회)
_TOOL_EXCLUDE = {"family": ("부양가족 수당", "부양가족수당")}


def tool_matches(name: str, words: tuple[str, ...], question: str) -> bool:
    for phrase in _TOOL_EXCLUDE.get(name, ()):
        question = question.replace(phrase, " ")
    return any(w in question for w in words)


def run_lookups(emp_id: str, question: str) -> tuple[dict, list[str]]:
    """항상 기본 정보(profile)를 넣고, 질문 단어에 맞는 도구를 추가로 실행. (결과, 사용한 도구 이름) 반환"""
    out, used = {"기본정보": get_profile(emp_id)}, ["profile"]
    for words, name, fn in TOOL_KEYWORDS:
        if tool_matches(name, words, question):
            r = fn(emp_id)
            if r:
                out[{"leave_balance": "연차", "welfare_points": "복지포인트", "payroll": "급여", "family": "가족",
                     "requests": "신청현황", "training": "교육"}[name]] = r
                used.append(name)
    return out, used
