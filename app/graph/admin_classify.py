"""인사팀(hr_admin) 질문 분류 (규칙). 역할은 토큰에서 오고, 이 분류는 hr_admin일 때만 써요.

의도
- data        : 집계·명단만 필요 → 집계 도구 → 답변
- both        : 집계 + 규정 (기준·기한·방법도 물음) → 집계 도구 + 검색 → 답변
- rule        : 규정·절차만 → 검색 → 답변 (징계·고충도 여기로. 로그만 [민감 주제 문의])
- unavailable : DB에 없는 데이터 → 안내
- refuse      : 주민번호·계좌·주소·연락처 요청 → 거절 (인사팀도 챗봇으로는 못 봄)

도구 고르기는 단어로 하고, 기간·기준값은 질문에서 뽑아요. 못 뽑으면 도구의 기본값을 써요.
"""
import re
from datetime import date, timedelta

SENSITIVE = ["괴롭힘", "성희롱", "성추행", "폭언", "징계", "고충", "따돌림", "부당", "갑질", "자살", "우울"]
PII_REQUEST = ["주민번호", "주민등록번호", "계좌", "주소", "전화번호", "연락처", "휴대폰"]
# 지금 DB나 도구로 답할 수 없는 데이터 (관리자 질문셋 '불가'). 출퇴근 기록(hr.attendance)은 근태 도구로 답해요.
UNAVAILABLE = ["유연근무", "초과근무 사전", "근로시간 단축", "통보 이력",
               "사용계획", "개인정보 조회했", "누가 조회", "특수건강진단"]
# 숫자만으로는 인사팀이 할 일을 모르는 도구 → 질문에 규정 단어가 없어도 근거 규정을 같이 검색 (연장근로 한도, 지각 처리, 촉진 절차)
#                 값은 규정 검색어에 덧붙일 말 (질문 문장만으로는 근거 조문이 잘 안 잡혀서)
ALWAYS_BOTH = {
    "weekly_hours_over": "연장근로의 제한 1주 12시간 한도",
    "late_early_frequent": "지각 조퇴 근무시작 소속장 승인",
    "attendance_missing": "근무상황부 근태 관리 퇴근절차",
    "leave_promotion_targets": "연차 유급휴가의 사용 촉진 미사용 휴가일수 서면 통보",
}
RULE_WORDS = ["규정", "기준", "언제까지", "어떻게", "절차", "해야", "방법", "몇 일 전", "며칠 전", "조건", "근거"]
LEAVE_TYPES = ["생리휴가", "보건휴가", "난임치료휴가", "병가", "공가", "청원", "장기재직", "포상휴가", "가족돌봄",
               "배우자출산", "출산전후", "대체휴가", "반차", "연차"]
REQUEST_TYPES = {"경조금": "경조금신청", "출장": "출장신청", "출산축하금": "출산축하금신청", "휴직": "휴직신청",
                 "복직": "복직신청", "휴가": "휴가신청"}
COURSE_TYPES = {"법정": "법정", "의무교육": "법정", "직무": "직무", "승진교육": "승진", "입문": "신규", "신규": "신규",
                "리더십": "리더십"}


# ───────────── 기간 ─────────────
def _month(y: int, m: int) -> tuple[date, date]:
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return date(y, m, 1), nxt - timedelta(days=1)


def period(q: str, today: date) -> tuple[date, date] | None:
    """질문 속 기간 표현 → (시작, 끝). 없으면 None"""
    monday = today - timedelta(days=today.weekday())
    if "다음 주" in q or "다음주" in q:
        return monday + timedelta(days=7), monday + timedelta(days=13)
    if "지난주" in q or "지난 주" in q:
        return monday - timedelta(days=7), monday - timedelta(days=1)
    if "이번 주" in q or "이번주" in q:
        return monday, monday + timedelta(days=6)
    if "다음 달" in q or "다음달" in q:
        return _month(today.year, today.month + 1)
    if "지난달" in q or "지난 달" in q:
        return _month(today.year, today.month - 1)
    if "이번 달" in q or "이번달" in q:
        return _month(today.year, today.month)
    if "분기" in q:
        qs = (today.month - 1) // 3 * 3 + 1
        return date(today.year, qs, 1), _month(today.year, qs + 2)[1]
    if ("올해" in q or "금년" in q) and ("내년" in q):
        return date(today.year, 1, 1), date(today.year + 1, 12, 31)
    if "내년" in q:
        return date(today.year + 1, 1, 1), date(today.year + 1, 12, 31)
    if "작년" in q or "지난해" in q:
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    if "올해" in q or "금년" in q:
        return date(today.year, 1, 1), date(today.year, 12, 31)
    m = re.search(r"최근\s*(\d+)\s*(년|개월|달)", q)
    if m:
        n = int(m.group(1))
        return today - timedelta(days=365 * n if m.group(2) == "년" else 30 * n), today
    return None


def _num(pattern: str, q: str, cast=int):
    m = re.search(pattern, q, re.I)
    return cast(m.group(1)) if m else None


def _first(words, q: str):
    return next((w for w in words if w in q), None)


# ───────────── 도구 고르기 ─────────────
def pick_tool(q: str, today: date) -> tuple[str | None, dict]:
    """질문 → (도구 이름, 인자). 맞는 도구가 없으면 (None, {})"""
    p = period(q, today)
    start, end = p if p else (None, None)
    year = start.year if p and start.year == end.year else None
    has = lambda *ws: any(w in q for w in ws)   # noqa: E731

    if has("결재", "대기", "승인 안", "처리 안", "미처리") and has("신청", "건"):
        rt = next((v for k, v in REQUEST_TYPES.items() if k in q), None)
        return "pending_requests", {"request_type": rt}

    if "복지포인트" in q or "복지 포인트" in q:
        if has("만료", "소멸", "남았"):
            return "welfare_points_expiring", {"asof": today}
        return "welfare_points_dept_usage", {"year": year}

    if "촉진" in q and has("대상", "명단", "누구", "직원", "부서", "필요", "몇 명"):
        return "leave_promotion_targets", {"asof": today, "min_days": _num(r"(\d+)\s*일\s*이상", q, float)}

    if has("52시간", "주 52", "연장근로 초과", "연장근로 한도 넘", "근로시간 초과"):
        return "weekly_hours_over", {"week_start": start or today, "limit_hours": 52}
    if has("지각", "조퇴") and not has("규정", "절차", "징계", "어떻게"):
        return "late_early_frequent", {"months": _num(r"(\d+)\s*(?:개월|달)", q) or 3,
                                       "min_count": _num(r"(\d+)\s*(?:회|번)", q) or 3}
    if has("출퇴근 기록", "출근 기록", "퇴근 기록") and has("빠진", "누락", "없는", "안 찍", "비어"):
        s = start or _month(today.year, today.month - 1)[0]
        return "attendance_missing", {"month": f"{s.year}-{s.month:02d}"}

    if "연차" in q and has("사용률", "평균", "부서별", "많이 쓴", "적게 쓴"):
        rate = _num(r"(\d+)\s*%", q)
        return "dept_leave_usage", {"year": year, "below_rate": rate / 100 if rate else None}
    if "연차" in q and has("남은", "잔여", "소멸", "TOP", "많은 직원", "명단"):
        return "leave_remaining_list", {"year": year, "min_days": _num(r"(\d+)\s*일\s*이상", q, float),
                                        "limit": _num(r"TOP\s*(\d+)", q)}

    if has("휴직 중", "휴직중", "휴직자", "복직 예정", "복직예정", "휴직상황"):
        if has("복직") and p:
            return "absence_list", {"asof": today, "return_from": start, "return_to": end}
        return "absence_list", {"asof": today}
    if "정년" in q:
        return "retirement_age_list", {"from_year": (start or today).year, "to_year": (end or today).year}
    if has("퇴사자", "퇴직자", "퇴직 유형", "퇴직유형") and has("부서", "유형", "몇 명", "인원", "많은"):
        s, e = p or (date(today.year, 1, 1), today)
        return "retirements_by_dept", {"start": s, "end": e}
    if "수습" in q:
        return "probation_ending", {"asof": today, "within_days": _num(r"(\d+)\s*일", q) or 30}
    if "입사자" in q or ("입사" in q and has("몇 명", "누구", "명단")):
        s, e = p or _month(today.year, today.month)
        return "hires_in_period", {"start": s, "end": e}
    ev = ("승진" if has("승진자", "승진한") else "근무형태변경" if "근무형태" in q else "전보" if has("전보자", "전보된") else None)
    if ev:
        s, e = p or (today - timedelta(days=365), today)
        return "hr_events", {"event_type": ev, "start": s, "end": e}

    if "교육" in q or "이수" in q:
        ct = next((v for k, v in COURSE_TYPES.items() if k in q), "법정")
        if "이수율" in q:
            return "dept_training_rate", {"year": year, "course_type": ct}
        if has("미이수", "안 들은", "안들은", "못 들은", "이수 안"):
            return "training_incomplete", {"year": year, "course_type": ct}

    if "실수령" in q and has("바뀐", "변한", "차이", "변화", "늘어난", "줄어든"):
        rate = _num(r"(\d+)\s*%", q)
        return "net_pay_change", {"threshold": rate / 100 if rate else None}
    if has("연장", "야간", "휴일근무", "시간외") and has("부서", "총액", "TOP", "많은"):
        pm = f"{start.year}-{start.month:02d}" if p and (end - start).days < 31 else None
        return "payroll_dept_totals", {"pay_month": pm}

    lt = _first(LEAVE_TYPES, q)
    if lt and p and has("부서", "몰린", "현황", "몇 명"):
        return "leave_by_dept_period", {"start": start, "end": end, "leave_type": None if lt == "연차" else lt}
    if lt and p and has("쓴 사람", "사용자", "명단", "누구"):
        return "leave_people_period", {"start": start, "end": end, "leave_type": lt}
    if has("휴가자", "휴가 몰린", "휴가가 몰린") and p:
        return "leave_by_dept_period", {"start": start, "end": end}

    return None, {}


def classify_admin(q: str, today: date | None = None) -> dict:
    """인사팀 질문 → {intent, admin_tool, admin_params, sensitive_topic}"""
    today = today or date.today()
    sensitive_topic = any(w in q for w in SENSITIVE)
    if any(w in q for w in PII_REQUEST) and not any(w in q for w in RULE_WORDS):
        return {"intent": "refuse", "sensitive_topic": sensitive_topic}
    if any(w in q for w in UNAVAILABLE):
        return {"intent": "unavailable", "sensitive_topic": sensitive_topic}
    tool, params = pick_tool(q, today)
    if tool:
        intent = "both" if tool in ALWAYS_BOTH or any(w in q for w in RULE_WORDS) else "data"
        out = {"intent": intent, "admin_tool": tool, "admin_params": params, "sensitive_topic": sensitive_topic}
        if tool in ALWAYS_BOTH:
            out["search_hint"] = ALWAYS_BOTH[tool]
        return out
    return {"intent": "rule", "sensitive_topic": sensitive_topic}
