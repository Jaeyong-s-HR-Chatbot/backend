"""인사팀(hr_admin) 질문 분류. 역할은 토큰에서 오고, 이 분류는 hr_admin일 때만 써요.

임직원 분류(nodes.classify)와 같은 순서로 하되 두 군데가 달라요.
1. 민감 단어 → 종료하지 않고 sensitive_topic 표시만 붙임 (검색·답변은 하고 로그만 [민감 주제 문의])
2. 다른 직원 언급 → 인사팀은 업무상 볼 수 있으니 거절하지 않음
3. 질문 내용 없음 → 규칙 결과로 종료
4. LLM 호출 → 의도·본인 조회 도구·계산 값 + 집계 도구를 쓸지와 그 인자까지 고름
5. LLM 실패 → 규칙 결과

의도
- rule         : 규정·절차만 → 검색 → 답변 (징계·고충도 여기로)
- lookup       : 담당자 본인 데이터만 (내 연차 잔여, 내 급여) → 본인 조회 → 답변
- both         : 데이터 + 규정. admin_tool이 있으면 집계 조회 + 규정 검색, 없으면 본인 조회 + 규정 검색
- out_of_scope : 인사 업무와 무관 → 안내
※ 인사팀에게는 refuse·sensitive 의도가 없어요 (임직원 전용)

규칙 분류는 집계 도구를 단어로 고르고, 기간·기준값은 질문에서 뽑아요. 못 뽑으면 도구의 기본값을 써요.
"""
import logging
import re
from datetime import date, timedelta

from .. import config

log = logging.getLogger("hr_chat")

SENSITIVE = ["괴롭힘", "성희롱", "성추행", "폭언", "고충", "따돌림", "부당", "갑질", "자살", "우울"]   # 징계는 민감 단어 아님
# 본인 질문: 1인칭이 띄어쓰기와 함께 나와야 해요. 임직원 규칙의 '남은·잔여'는 인사팀 집계 질문("남은 연차 10일 이상인 직원")에도
# 흔해서 쓰지 않아요.
SELF = re.compile(r"(?<![가-힣A-Za-z0-9])(?:내|제|나의|저의|내가|제가)(?=\s)")
# 1인칭이 있어도 여러 사람을 묻는 말이면 집계 질문 ("내 팀원들 연차", "제 부서 직원 명단")
GROUP_WORDS = ["직원", "명단", "부서", "팀원", "사람", "누구", "몇 명", "전체", "TOP"]
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
}
LEAVE_TYPES = ["생리휴가", "보건휴가", "난임치료휴가", "병가", "공가", "청원", "장기재직", "포상휴가", "가족돌봄",
               "배우자출산", "출산전후", "대체휴가", "반차", "연차"]
REQUEST_TYPES = {"경조금": "경조금신청", "출장": "출장신청", "출산축하금": "출산축하금신청", "휴직": "휴직신청",
                 "복직": "복직신청", "휴가": "휴가신청"}
COURSE_TYPES = {"법정": "법정", "의무교육": "법정", "직무": "직무", "승진교육": "승진", "입문": "신규", "신규": "신규",
                "리더십": "리더십"}
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
    if has("교육", "이수", "과정") and has("미이수", "안 들은", "안들은", "못 들은", "이수 안"):
        ct = next((v for k, v in COURSE_TYPES.items() if k in q), "법정")
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


def _both(tool: str, params: dict, sensitive_topic: bool) -> dict:
    # 집계 도구를 쓰면 규정 검색도 항상 같이 해요. 규정을 답에 쓸지는 근거를 보는 답변 LLM이 정해요.
    out = {"intent": "both", "admin_tool": tool, "admin_params": params, "sensitive_topic": sensitive_topic}
    if tool in SEARCH_HINTS:
        out["search_hint"] = SEARCH_HINTS[tool]
    return out


def classify_admin(q: str, today: date | None = None) -> dict:
    """인사팀 규칙 분류: 질문 → {intent, admin_tool?, admin_params?, search_hint?, sensitive_topic}

    순서: 민감 표시 → (다른 직원 허용) → 본인 질문 → 집계 도구 → out_of_scope → rule
    """
    from .nodes import _APPLICATION_STATUS, _HR_WORDS, _OTHER_ORG, _RULE_WORDS, _TOOL_WORDS   # 순환 import 방지

    today = today or date.today()
    sensitive_topic = any(w in q for w in SENSITIVE)        # 1. 종료하지 않고 표시만
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


def classify_admin_state(s: dict, today: date | None = None) -> dict:
    """인사팀 분류 진입점 (nodes.classify가 hr_admin일 때 불러요). CLASSIFY_MODE는 임직원과 같은 값을 써요.
    today는 평가에서 기준일을 고정할 때만 넘겨요."""
    from .nodes import has_question_content, question_text   # 순환 import 방지

    today = today or date.today()
    question = question_text(s)
    rule_result = classify_admin(question, today)
    if config.CLASSIFY_MODE == "rule":
        return rule_result
    if not has_question_content(s["masked_question"]):      # 3. 질문 내용 없음 → 규칙 결과로 종료
        return rule_result

    from . import admin_llm_classify

    result = admin_llm_classify.classify(question, today)   # 4. LLM
    if result is None:                                              # 5. 실패 → 규칙 결과
        log.warning("인사팀 LLM 의도 분류에 실패해 규칙 결과로 대체합니다")
        return rule_result
    sensitive_topic = rule_result["sensitive_topic"]
    if config.CLASSIFY_MODE == "assist":
        # 의도·도구는 규칙 그대로 두고, LLM은 계산 값 추출에만 써요 (임직원 assist와 같음)
        return {**rule_result, "classifier_calc": result["calc"]}
    if result.get("admin_tool"):
        out = _both(result["admin_tool"], result["admin_params"], sensitive_topic)
    else:
        out = {"intent": result["intent"], "sensitive_topic": sensitive_topic}
    out["classifier_tools"] = result["tools"]
    out["classifier_calc"] = result["calc"]
    return out
