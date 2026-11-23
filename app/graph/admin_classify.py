"""인사팀(hr_admin) 질문 분류. 역할은 토큰에서 오고, 이 분류는 hr_admin일 때만 써요.

임직원 분류(nodes._classify_rule)와 같은 순서로 하되 두 군데가 달라요.
1. 민감 단어 → 종료하지 않고 sensitive_topic 표시만 붙임 (검색·답변은 하고 로그만 [민감 주제 문의])
2. 다른 직원 언급 → 인사팀은 업무상 볼 수 있으니 거절하지 않음
의도·집계 도구는 규칙으로 정하고, 규칙이 못 뽑은 기간·기준값만 분류 보조 LLM으로 채워요 (admin_params_llm).

의도
- rule         : 규정·절차만 → 검색 → 답변 (징계·고충도 여기로)
- lookup       : 담당자 본인 데이터만 (내 연차 잔여, 내 급여) → 본인 조회 → 답변
- both         : 데이터 + 규정. admin_tool이 있으면 집계 조회 + 규정 검색, 없으면 본인 조회 + 규정 검색
- out_of_scope : 인사 업무와 무관 → 안내
※ 인사팀에게 sensitive 의도는 없어요. refuse는 대화 기록 정책에만 써요:
  특정 직원이 챗봇에 물어본 내용, 민감 상담·신고를 한 직원이 누구인지는 인사팀에게도 알려주지 않음 (주제별 집계는 허용)

규칙 분류는 집계 도구를 단어로 고르고, 기간·기준값은 질문에서 뽑아요. 못 뽑으면 도구의 기본값을 써요.
"""
import logging
import re
from datetime import date, timedelta

log = logging.getLogger("hr_chat")

SENSITIVE = ["괴롭힘", "성희롱", "성추행", "폭언", "고충", "따돌림", "부당", "갑질", "자살", "우울"]   # 징계는 민감 단어 아님
# 본인 질문: 1인칭이 띄어쓰기와 함께 나와야 해요. 임직원 규칙의 '남은·잔여'는 인사팀 집계 질문("남은 연차 10일 이상인 직원")에도
# 흔해서 쓰지 않아요.
SELF = re.compile(r"(?<![가-힣A-Za-z0-9])(?:내|제|나의|저의|내가|제가)(?=\s)")
# 대화 기록 정책 (관리자 골든셋 A-L01~L04): 주제 집계는 허용, 특정인의 대화·민감 상담자 신원은 거절
_TITLE = r"(?:사원|주임|대리|과장|차장|부장|팀장|본부장|님|씨)"
CHAT_LOG = re.compile(r"챗봇|대화\s*(?:기록|내용|내역|로그)|채팅\s*(?:기록|내용|내역)|질문\s*(?:기록|내역|로그)")
WHO = re.compile(rf"누가|누구(?!\s*(?:한테|에게|랑|와|에))|명단|이름|사번|[Ee]1\d{{3}}|\[사번\]|[가-힣]{{2,4}}\s?{_TITLE}(?:이|가|은|는|의)?\s")
# 민감 상담·신고를 "한 사람"을 묻는 말 ("괴롭힘 상담한 직원"). "신고는 누구한테 해?"는 절차 질문이라 아님
CONSULTER = re.compile(r"(?:상담|문의|신고|제보|물어|질문)\S*\s*(?:한|했던|하신|했다는)\s*(?:직원|사람|사원|분|팀원)|(?:상담|신고|제보)자")

# 1인칭이 있어도 여러 사람을 묻는 말이면 집계 질문 ("내 팀원들 연차", "제 부서 직원 명단")
GROUP_WORDS = ["직원", "명단", "부서", "팀원", "사람", "누구", "몇 명", "전체", "TOP"]
# 근속 연수 목록: "근속 10·20·30년", "근속 25년", "근속 10, 20년" (연도 "2026년"은 안 잡게 1~2자리 숫자만)
SERVICE_YEARS = re.compile(r"근속\s*((?<!\d)\d{1,2}(?:\s*[·,/]\s*\d{1,2})*)\s*년")
# 인사팀 업무 단어. 임직원 규칙의 인사 단어(_HR_WORDS)에 더해, 이것도 없고 집계 도구도 못 찾으면 out_of_scope
ADMIN_HR_WORDS = ["지각", "조퇴", "출퇴근", "출근", "퇴근", "정년", "수습", "촉진", "근로", "통보", "퇴사", "복직",
                  "이수율", "노사", "단체협약", "취업규칙", "위원회", "채용", "전보", "발령", "인원",
                  "훈련", "겸직", "복무", "선발", "실태조사"]
# 도구별 규정 검색어 보강 (질문 문장만으로는 근거 조문이 잘 안 잡히는 도구: 연장근로 한도, 지각 처리, 촉진 절차)
SEARCH_HINTS = {
    "weekly_hours_over": "연장근로의 제한 1주 12시간 한도",
    "late_early_frequent": "지각 조퇴 근무시작 소속장 승인",
    "attendance_missing": "근무상황부 근태 관리 퇴근절차",
    "leave_promotion_targets": "연차 유급휴가의 사용 촉진 미사용 휴가일수 서면 통보",
    "unused_leave_pay_estimate": "연차 미사용 수당 보상 통상임금",
    "new_hire_monthly_leave": "1년 미만 근로자 1개월 개근 유급휴가 연차",
    "sick_leave_over_limit": "병가 연간 한도 업무상 질병 병가",
    "long_service_leave_unused": "장기재직휴가 근속 10년 20년 30년",
    "childcare_leave_eligible_unused": "육아휴직 만 8세 이하 자녀",
    "retiree_leave_payout": "퇴직 연차 미사용 수당 정산",
    "night_work_heavy": "야간작업 특수건강진단 대상",
    "severance_estimate": "퇴직급여 평균임금 지급",
    "honorary_retirement_eligible": "명예퇴직 신청 자격 근속",
    "wage_peak_list": "임금피크제 적용 대상 정년 2년 전",
    "expected_births": "출산전후휴가 90일 임신 근로자",
    "birth_bonus_targets": "출산축하금 지급 대상 금액",
    "promotion_min_years_met": "승진 소요 최저연수 직급별",
    "exam_promotion_4_candidates": "4급 시험승진 응시 자격 5급 재직연수",
    "long_stay_same_dept": "순환전보 같은 부서 근무 기간",
    "transfer_restricted": "전보제한 기간 채용 구분",
    "training_hours_lowest": "교육훈련 이수 시간 기준",
    "family_allowance_overage": "가족수당 자녀 연령 지급 대상",
    "long_service_bracket_change": "장기근속수당 근속연수 구간 지급 기준",
    "couple_family_allowance": "가족수당 부부 직원 중복 지급 제한",
    "welfare_points_proration_check": "복지포인트 중도 입사 복직 월할 지급",
    "service_anniversary": "근속 표창 근속기간 10년 20년 30년 포상",
    "trips_by_dept": "국내여비 정액표 일비 식비 숙박비",
    "trips_unsettled": "여비 정산 출장 후 정산서 제출",
    "long_trip_per_diem_check": "장기 출장 일비 감액 같은 지역 15일 초과",
    "duplicate_condolence_payments": "경조금 중복 지급 금지 같은 경조사",
}
LEAVE_TYPES = ["생리휴가", "보건휴가", "난임치료휴가", "병가", "공가", "청원", "장기재직", "포상휴가", "가족돌봄",
               "배우자출산", "출산전후", "대체휴가", "반차", "연차"]
REQUEST_TYPES = {"경조금": "경조금신청", "출장": "출장신청", "출산축하금": "출산축하금신청", "휴직": "휴직신청",
                 "복직": "복직신청", "휴가": "휴가신청"}
COURSE_TYPES = {"법정": "법정", "의무교육": "법정", "직무": "직무", "승진교육": "승진", "입문": "신규", "신규": "신규",
                "리더십": "리더십"}
# 교육 과정 이름: "기관사 보수교육", "개인정보보호 교육", "승객 응대 실무", "비상 상황 대응 훈련"
_COURSE = re.compile(r"((?:[가-힣A-Za-z]+\s*){0,3}?[가-힣A-Za-z]*(?:교육|과정|실무|훈련))")
_COURSE_LEAD = re.compile(r"^(?:(?:올해|작년|이번|아직|필수|우리|전체|제|내|부서|팀)\s*)+")
_COURSE_GENERIC = {"교육", "과정", "필수교육", "의무교육", "교육과정"}


def _course_name(q: str) -> str | None:
    """질문의 교육 과정 이름. 구분 단어(법정·신규·입문·승진·리더십·직무)가 들어 있거나 '교육'뿐이면 None (구분으로 거름)"""
    m = _COURSE.search(q)
    if not m:
        return None
    name = _COURSE_LEAD.sub("", m.group(1).strip()).strip()
    if name.replace(" ", "") in _COURSE_GENERIC or any(k in name for k in COURSE_TYPES):
        return None
    return name


# 규정만 묻는 말 ("보충해도 돼?", "줄여줄 수 있어?", "배정 기준"). 사람·집계를 묻는 말(DATA_WORDS)이 없으면 도구를 안 써요
RULE_ASK = re.compile(r"해도\s*(?:돼|되)|수\s*있(?:어|나|는\s*경우|을까)|가능|기준|얼마\s*(?:줘|주|받)|해야\s*(?:돼|되|하)|되나|되는지")
DATA_WORDS = ["명단", "누구", "몇 명", "인원", "현황", "대상자", "목록", "리스트", "직원", "사람", "부서별", "TOP",
              "뽑아", "보여", "총액", "사용률", "이수율", "건수", "몇 건"]


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

    if RULE_ASK.search(q) and not has(*DATA_WORDS):     # 규정만 묻는 질문은 도구 없이 rule로
        return None, {}

    # 규정 단어가 겹치는 기존 규칙(연차 남은·퇴직자·야간·교육·복지포인트·휴가 종류 등)보다 앞에서 먼저 확인해요
    tool = _pick_extra_tool(q, today, p, start, end, has)
    if tool:
        return tool

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
    # 퇴직 유형별 인원은 "정년"이 예시로 들어가도 퇴직 집계 ("퇴직 유형별(정년·의원 등) 인원")
    if has("퇴사자", "퇴직자", "퇴직 유형", "퇴직유형") and has("부서", "유형", "몇 명", "인원", "많은"):
        s, e = p or (date(today.year, 1, 1), today)
        return "retirements_by_dept", {"start": s, "end": e}
    if "정년" in q:
        return "retirement_age_list", {"from_year": (start or today).year, "to_year": (end or today).year}
    if "수습" in q:
        return "probation_ending", {"asof": today, "within_days": _num(r"(\d+)\s*일", q) or 30}
    # 교육 미이수는 입사자 단어보다 먼저 ("신규입사자 입문 과정 안 들은 사람")
    if has("교육", "이수", "과정", "실무", "훈련") and has("미이수", "안 들은", "안들은", "못 들은", "이수 안"):
        ct = next((v for k, v in COURSE_TYPES.items() if k in q), "법정")
        name = _course_name(q)
        if name:                                   # "기관사 보수교육 대상인데 안 들은 사람" → 그 과정만
            return "training_incomplete", {"year": year, "course_name": name}
        return "training_incomplete", {"year": year, "course_type": ct}
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

    if "실수령" in q and has("바뀐", "변한", "차이", "변화", "늘어난", "줄어든"):
        rate = _num(r"(\d+)\s*%", q)
        return "net_pay_change", {"threshold": rate / 100 if rate else None}
    if has("연장", "야간", "휴일근무", "시간외") and has("부서", "총액", "TOP", "많은"):
        pm = f"{start.year}-{start.month:02d}" if p and (end - start).days < 31 else None
        return "payroll_dept_totals", {"pay_month": pm}

    lt = _first(LEAVE_TYPES, q)
    if lt and has("부서", "몰린", "현황", "몇 명") and (p or has("현황", "부서별")):
        s, e = p or (date(today.year, 1, 1), today)          # 기간이 없으면 올해 1월 1일부터 오늘까지
        types = [t for t in LEAVE_TYPES if t in q and t != "연차"]
        leave_type = None if not types else types[0] if len(types) == 1 else types
        return "leave_by_dept_period", {"start": s, "end": e, "leave_type": leave_type}
    if lt and p and has("쓴 사람", "사용자", "명단", "누구"):
        return "leave_people_period", {"start": start, "end": end, "leave_type": lt}
    if has("휴가자", "휴가 몰린", "휴가가 몰린") and p:
        return "leave_by_dept_period", {"start": start, "end": end}

    return None, {}


def _pick_extra_tool(q: str, today: date, p, start, end, has) -> tuple[str, dict] | None:
    """집계 도구 28개용 규칙. 순서가 중요해요 (위에서부터): 더 구체적인 단어가 먼저."""
    year = start.year if p and start.year == end.year else None
    this_month = f"{start.year}-{start.month:02d}" if p and (end - start).days < 31 else None

    # 절차·방법만 묻는 말은 규정 질문 ("명예퇴직 신청 자격이 어떻게 돼?", "전보제한 해제 신청 절차")
    if has("절차", "어떻게", "줄여줘야", "해제", "방법") and not has("명단", "누구", "몇 명", "직원", "사람", "목록"):
        return None

    # 휴가
    if "연차수당" in q and has("미사용", "예상", "총액", "얼마"):
        return "unused_leave_pay_estimate", {"year": year}
    if has("퇴직자", "퇴사자") and has("연차", "휴가") and has("정산", "수당", "금액"):
        return "retiree_leave_payout", {"year": year}
    if "입사 1년" in q and has("연차", "월 단위", "월단위"):
        return "new_hire_monthly_leave", {"asof": today}
    if "병가" in q and has("한도", "넘긴", "초과", "넘은"):
        return "sick_leave_over_limit", {"year": year}
    if "장기재직" in q and has("안 쓴", "안쓴", "미사용", "대상"):
        return "long_service_leave_unused", {"year": year}
    if "육아휴직" in q and has("쓸 수 있", "안 쓴", "안쓴", "미사용"):
        return "childcare_leave_eligible_unused", {"asof": today}
    # 근태·퇴직·출산
    if "야간" in q and has("특수건강진단", "특검", "건강진단 대상"):
        return "night_work_heavy", {"months": 6}
    if "임금피크" in q:
        return "wage_peak_list", {"asof": today}
    if "퇴직금" in q and has("예상", "얼마", "추정", "예정자"):
        return "severance_estimate", {"retire_from": today, "retire_to": date(today.year + 1, 12, 31)}
    if "명예퇴직" in q and has("가능", "대상", "몇 명", "자격", "누구"):
        return "honorary_retirement_eligible", {"asof": today}
    if "출산축하금" in q and has("대상", "지급", "누구"):
        return "birth_bonus_targets", {"month": this_month or f"{today.year}-{today.month:02d}"}
    if "출산" in q and has("예정", "곧", "임박"):
        return "expected_births", {"asof": today}
    # 승진·전보
    if "시험승진" in q or ("4급" in q and has("응시", "승진")):
        return "exam_promotion_4_candidates", {"asof": today}
    if "승진" in q and has("최저연수", "소요 연수", "소요연수", "최저 연수"):
        return "promotion_min_years_met", {"year": year or today.year}
    if "전보제한" in q or "전보 제한" in q:
        return "transfer_restricted", {"asof": today}
    if has("순환전보", "순환 전보") or ("전보" in q and has("오래", "장기간")) or ("부서" in q and has("오래 있", "오래 근무")):
        return "long_stay_same_dept", {"asof": today}
    # 교육
    if "교육시간" in q and has("적은", "낮은", "부족", "제일", "가장"):
        return "training_hours_lowest", {"year": year, "limit": 10}
    # 수당·복지
    if "가족수당" in q and has("부부", "둘 다", "둘다", "중복"):
        return "couple_family_allowance", {}
    if "가족수당" in q and has("기준 나이", "나이 넘", "넘은", "초과", "자격"):
        return "family_allowance_overage", {"asof": today}
    if "장기근속수당" in q and has("구간", "바뀌", "변경", "올라"):
        ym = f"{start.year}-{start.month:02d}" if p else (f"{today.year + (today.month == 12)}-{today.month % 12 + 1:02d}")
        return "long_service_bracket_change", {"month": ym}
    if has("복지포인트", "복지 포인트") and has("월할", "제대로", "맞게", "중간에 입사", "복직"):
        return "welfare_points_proration_check", {"year": year}
    if "근속" in q and has("10·20·30", "10,20,30", "10 20 30", "되는 직원", "되는 사람") and "수당" not in q:
        params = {"year": year or today.year}
        years = SERVICE_YEARS.search(q)          # "근속 10·20·30년", "근속 25년" → [10, 20, 30], [25] (없으면 도구 기본 10·20·30)
        if years:
            params["years"] = [int(n) for n in re.findall(r"\d+", years.group(1))]
        return "service_anniversary", params
    # 출장·경조금
    if "출장" in q and has("일비") and has("감액", "장기"):
        return "long_trip_per_diem_check", {}
    if "출장" in q and has("정산"):
        return "trips_unsettled", {"asof": today}
    if "출장" in q and has("부서", "출장비") and has("많은", "예상", "총액", "TOP"):
        return "trips_by_dept", {"month": this_month or f"{today.year}-{today.month:02d}"}
    if "경조금" in q and has("두 번", "두번", "중복", "이중", "또 나간"):
        return "duplicate_condolence_payments", {}
    # 로그
    if has("챗봇", "대화") and has("주제", "직원들", "주로", "TOP") and not has("괴롭힘", "고충"):
        return "chat_topic_stats", {"month": this_month, "limit": _num(r"TOP\s*(\d+)", q) or 5}
    if has("개인정보", "조회 기록", "감사로그", "감사 로그") and has("조회", "열람", "누가"):
        return "audit_log_recent", {"action": "개인정보 조회" if "개인정보" in q else None}
    return None


def _both(tool: str, params: dict, sensitive_topic: bool) -> dict:
    # 집계 도구를 쓰면 규정 검색도 항상 같이 해요. 규정을 답에 쓸지는 근거를 보는 답변 LLM이 정해요.
    out = {"intent": "both", "admin_tool": tool, "admin_params": params, "sensitive_topic": sensitive_topic}
    if tool in SEARCH_HINTS:
        out["search_hint"] = SEARCH_HINTS[tool]
    return out


def classify_admin(q: str, today: date | None = None) -> dict:
    """인사팀 규칙 분류: 질문 → {intent, admin_tool?, admin_params?, search_hint?, sensitive_topic}

    순서: 민감 표시 → 대화 기록 정책(특정인 대화·민감 상담자 신원 → refuse) → (다른 직원 허용) → 본인 질문 → 집계 도구 → out_of_scope → rule
    """
    from .nodes import _APPLICATION_STATUS, _HR_WORDS, _OTHER_ORG, _RULE_WORDS, _TOOL_WORDS   # 순환 import 방지

    today = today or date.today()
    sensitive_topic = any(w in q for w in SENSITIVE)        # 1. 종료하지 않고 표시만
    if WHO.search(q) and (CHAT_LOG.search(q) or (sensitive_topic and CONSULTER.search(q))):
        return {"intent": "refuse", "refuse_reason": "chat_log", "sensitive_topic": sensitive_topic}   # 0. 대화 기록 정책
    # 2. 다른 직원 언급은 거절하지 않음 (인사팀 업무)
    if SELF.search(q) and not any(w in q for w in GROUP_WORDS):
        has_tool = any(w in q for w in _TOOL_WORDS) or bool(_APPLICATION_STATUS.search(q))
        asks_rule = any(w in q for w in _RULE_WORDS)
        return {"intent": "lookup" if has_tool and not asks_rule else "both", "sensitive_topic": sensitive_topic}
    tool, params = pick_tool(q, today)
    if tool:
        return _both(tool, params, sensitive_topic)
    if _OTHER_ORG.search(q) or not any(w in q for w in (*_HR_WORDS, *ADMIN_HR_WORDS)):
        return {"intent": "out_of_scope", "sensitive_topic": sensitive_topic}
    return {"intent": "rule", "sensitive_topic": sensitive_topic}


def classify_admin_state(s: dict, today: date | None = None, use_llm: bool | None = None) -> dict:
    """인사팀 분류 진입점 (nodes.classify가 hr_admin일 때 불러요).

    의도·집계 도구는 규칙이 정하고, 도구가 정해지면 ③ 그 도구의 기간·기준값만 분류 보조 LLM에 다시 물어
    규칙이 비워 둔 인자만 형식이 맞는 LLM 값으로 채워요 (실패하면 규칙 값). today·use_llm은 평가에서만 넘겨요."""
    from . import admin_params_llm, llm_classify   # 순환 import 방지
    from .nodes import question_text

    today = today or date.today()
    question = question_text(s)
    result = classify_admin(question, today)
    use_llm = llm_classify.enabled() if use_llm is None else use_llm
    if use_llm and result.get("admin_tool"):
        params = admin_params_llm.extract(question, result["admin_tool"], today)
        if params is not None:   # 규칙이 비워 둔 인자만 채움 (규칙 값을 덮어쓰면 맞던 값이 틀어졌음: 휴가 종류 목록, 기본 월)
            merged = dict(result.get("admin_params", {}))
            for key, value in params.items():
                if merged.get(key) is None:
                    merged[key] = value
            result["admin_params"] = merged
    return result
