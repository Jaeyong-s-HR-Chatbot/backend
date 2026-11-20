"""파이프라인 단계(노드). DB·LLM 주소가 없으면 가짜 값으로 동작해요.

각 노드는 state를 받아 바뀐 값만 dict로 돌려줘요.
진짜로 바꿀 때는 함수 안만 고치고, 이름과 입출력(HRState 필드)은 그대로 둬요.
"""
import json
import logging
import re
from datetime import date
from decimal import Decimal
from urllib.parse import urlsplit

import httpx

from .. import config
from .. import calc_rules
from ..fake_data import HR_RECORDS
from ..masking import mask_answer, mask_text, pseudonymize
from . import llm_classify
from . import retriever
from .admin_classify import classify_admin
from .state import HRState

log = logging.getLogger("hr_chat")

VERIFY_MIN_SCORE = config.VERIFY_MIN_SCORE   # 근거 점수가 이보다 낮으면 재검색 → 그래도 낮으면 거절
MAX_RETRIES = 1
LOOKUP_ANSWER_LOG_PLACEHOLDER = "[본인 조회 답변 · 내용 미저장]"
ADMIN_ANSWER_LOG_PLACEHOLDER = "[인사팀 집계 답변 · 내용 미저장]"
SENSITIVE_PSEUDO_LOG_PLACEHOLDER = "[민감 주제 · 가명 미저장]"

def guard(s: HRState) -> HRState:
    result = mask_text(s["question"], own_emp_id=s["employee_id"], own_name=s.get("own_name"))
    return {
        "masked_question": result.text,
        "pii_found": result.found,
        "has_other_emp_id": result.found.get("사번", 0) > 0,
        "retries": 0,
    }


# ───────────── classify: 의도 분류 (규칙) ─────────────
_SENSITIVE = ["괴롭힘", "성희롱", "성추행", "폭언", "징계", "고충", "따돌림", "부당"]
_OTHER_PERSON = ["다른 직원", "다른 사람", "남의 ", "옆팀", "옆 팀"]
_TITLE = r"(?:사원|주임|대리|과장|차장|부장|팀장|본부장|님|씨)"
_PRIVATE = r"(?:연봉|급여|월급|연차|수당|평가|인사고과|주민번호|계좌|주소|전화번호|휴직)"
_OTHER_NAME = re.compile(rf"[가-힣]{{2,4}}\s?{_TITLE}(?:님)?(?:의|은|는|이|가)?\s?{_PRIVATE}")
_LOG_OTHER_NAME = re.compile(rf"(?<![가-힣A-Za-z0-9])(?P<name>[가-힣]{{2,4}})(?P<title>\s*{_TITLE}(?:님)?)")
_EMP_ID = re.compile(r"(?<![A-Za-z0-9])[Ee]1\d{3}(?!\d)")
_SELF = re.compile(
    r"(?<![가-힣A-Za-z0-9])(?:내\s|나의|제\s|저의|내가|제가|나 연차|저 연차)"
    r"|남은|남았|잔여"
)
_PERSONAL_IDENTIFIER_INTRO = re.compile(
    r"(?:제|내|저의|나의)\s*"
    r"(?:전화번호|번호|연락처|사번|이메일|메일|계좌번호|계좌|주민번호|카드번호)"
    r"\s*(?:은|는|이|가|을|를|의|에|으로|로)?\s*"
    r"\[(?:전화|본인사번|이메일|계좌|주민번호|카드)\]"
)
_PERSONAL_MASK_TAG = re.compile(
    r"\[(?:전화|이메일|계좌|주민번호|카드|본인사번|본인이름)\]"
)
_IDENTIFIER_LABEL = re.compile(
    r"(?:연락처|전화번호|번호|이메일|메일|계좌번호|계좌|주민번호|카드번호|사번)"
)
_SELF_IDENTIFIER_LABEL = re.compile(
    r"(?:제|내|저의|나의)\s*(?:연락처|전화번호|번호|이메일|메일|계좌번호|계좌|주민번호|카드번호|사번)"
)
_IDENTIFIER_DECLARATION_END = re.compile(
    r"(?:이에요|예요|이예요|입니다|이랍니다|거예요)\s*[.!?]*$"
)
_NO_QUESTION_CONTENT = re.compile(
    r"^[\s\W_]*(?:(?:이에요|이예요|예요|입니다|이랍니다|거예요|거에요|인데요|이고요|인데|이고|이며)[\s\W_]*)*$"
)
_APPLICATION_STATUS = re.compile(
    r"(?:신청|결재|승인|접수)\s*(?:현황|상태|내역|결과|여부|결재|승인|처리|됐|되었|완료)"
    r"|(?:결재|승인)\s*(?:됐|되었|완료|여부|상태|결과)"
)
_FUTURE_APPLICATION = re.compile(
    r"(?:신청|접수)\s*(?:하려고|하려면|하려는|하고자|할까|할까요|할\s*때|하고\s*싶|"
    r"방법|절차|어떻게|하는\s*방법)"
)
# 인사 규정·본인 인사 데이터와 관련된 단어. 하나도 없으면 out_of_scope (날씨·잡담·재테크 등)
_HR_WORDS = [
    "휴가", "연차", "휴직", "병가", "공가", "수당", "급여", "월급", "보수", "봉급", "상여", "임금", "연봉",
    "퇴직", "복지", "포인트", "교육", "이수", "출장", "여비", "경조", "결혼", "출산", "육아", "돌봄",
    "사망", "부고", "이사", "이전비", "입학", "규정", "징계", "근무", "야근", "당직", "승진", "평가",
    "인사", "입사", "가족", "부양", "진단서", "결재", "승인", "소명", "정산", "통상", "시간외", "야간",
    "휴일", "사번", "직급", "직원", "직장", "회사", "팀장", "팀원", "동료", "상사", "관리자", "업무",
    "대화방", "모욕", "고충", "신청",
]
# 다른 조직의 제도를 묻는 질문 (우리 회사 규정으로 답할 수 없음)
_OTHER_ORG = re.compile(r"공무원|공공기관|타사|다른\s*회사|남의\s*회사|대기업|삼성|현대|엘지|LG|SK|카카오|네이버")
_RULE_WORDS = ["규정", "기준", "조건", "받을 수", "어떻게", "방법", "절차", "계산", "왜"]
_TOOL_WORDS = ["연차", "휴가 며칠 남", "복지포인트", "복지 포인트", "포인트", "급여", "월급", "실수령", "통상임금", "수당 얼마 나왔",
               "가족수당", "부양가족", "교육", "이수"]   # app/tools.py의 조회 도구와 같은 단어


def _strip_personal_identifier_declarations(question: str) -> str:
    """의도 판정에서 단독 식별자 소개 절을 제외하고 나머지 질문은 보존한다."""
    parts = re.split(r"(?<=[.!?;\n])", question)
    kept = []
    for part in parts:
        has_personal_tag = bool(_PERSONAL_MASK_TAG.search(part))
        introduces_identifier = (
            bool(_IDENTIFIER_LABEL.search(part))
            or bool(_SELF_IDENTIFIER_LABEL.search(part))
            or "[본인이름]" in part
        )
        if (
            has_personal_tag
            and introduces_identifier
            and _IDENTIFIER_DECLARATION_END.search(part.strip())
        ):
            continue
        kept.append(part)
    return "".join(kept)


def _intent_question(question: str) -> str:
    question = _PERSONAL_IDENTIFIER_INTRO.sub("", question)
    return _strip_personal_identifier_declarations(question)


def has_question_content(question: str) -> bool:
    return not _NO_QUESTION_CONTENT.fullmatch(_intent_question(question))


def _classify_rule(s: HRState) -> HRState:
    """직원(employee) 규칙 분류. 인사팀(hr_admin)은 classify()에서 admin_classify로 먼저 빠져요"""
    q = s["masked_question"]
    intent_q = _intent_question(q)
    other_id = s.get("has_other_emp_id", False) or "[사번]" in q
    has_read_tool = any(w in intent_q for w in _TOOL_WORDS)
    has_application_status = bool(_APPLICATION_STATUS.search(intent_q))
    has_tool = has_read_tool or has_application_status
    is_self = bool(_SELF.search(intent_q)) or ("[본인사번]" in q and has_tool)
    future_application_only = (
        bool(_FUTURE_APPLICATION.search(intent_q))
        and not has_read_tool
        and not has_application_status
    )
    asks_rule = any(w in intent_q for w in _RULE_WORDS)
    if any(w in q for w in _SENSITIVE):
        intent = "sensitive"
    elif other_id or any(w in q for w in _OTHER_PERSON) or _OTHER_NAME.search(q):
        intent = "refuse"          # 타인 조회·권한 밖
    elif _OTHER_ORG.search(intent_q) or (
        not any(w in intent_q for w in _HR_WORDS)
        and not _PERSONAL_MASK_TAG.search(q)   # 개인정보 소개 문장은 기존대로 (질문 없음 처리)
    ):
        intent = "out_of_scope"    # 인사 규정과 무관하거나 다른 조직의 제도
    elif is_self and has_tool and not asks_rule:
        intent = "lookup"          # 내 데이터만 보면 되는 질문 (연차 잔여, 급여, 신청 현황 등)
    elif is_self and not future_application_only:
        intent = "both"            # 내 조건 + 규정 (조회 결과와 근거를 함께 봄)
    else:
        intent = "rule"
    return {"intent": intent}


def classify(s: HRState) -> HRState:
    if s.get("role") == "hr_admin":                # 인사팀은 전용 분류 (역할은 토큰에서)
        return classify_admin(s["masked_question"])
    if config.CLASSIFY_MODE == "rule":
        return _classify_rule(s)

    question = s["masked_question"]
    rule_result = None
    if config.CLASSIFY_MODE in {"hybrid", "assist"}:
        rule_result = _classify_rule(s)
        if rule_result["intent"] in {"sensitive", "refuse"}:
            return rule_result
        if not has_question_content(question):
            return rule_result
    elif not has_question_content(question):
        return {"intent": "rule"}

    result = llm_classify.classify(question, date.today())
    if result is None:
        log.warning("LLM 의도 분류에 실패해 규칙 결과로 대체합니다")
        return rule_result or _classify_rule(s)

    if result["intent"] in {"sensitive", "refuse"}:
        return {
            "intent": result["intent"],
            "classifier_tools": [],
            "classifier_calc": None,
        }
    if config.CLASSIFY_MODE == "assist":
        # 의도와 조회 도구는 규칙 그대로 두고, LLM은 민감·타인 조회 보완과 계산 값 추출에만 써요
        return {"intent": rule_result["intent"], "classifier_calc": result["calc"]}
    return {
        "intent": result["intent"],
        "classifier_tools": result["tools"],
        "classifier_calc": result["calc"],
    }


# ───────────── lookup: 본인 조회 ─────────────
def lookup(s: HRState) -> HRState:
    """사번은 반드시 토큰에서 온 s['employee_id']만 사용. DB가 있으면 app/tools.py의 정해진 조회만 실행"""
    if config.USE_DB:
        from .. import tools
        selected_tools = s.get("classifier_tools")
        data, used = tools.run_lookups(
            s["employee_id"],
            s["masked_question"],
            selected_tools=selected_tools if isinstance(selected_tools, list) else None,
        )
    else:
        record = dict(HR_RECORDS.get(s["employee_id"], {}))
        selected_tools = s.get("classifier_tools")
        if isinstance(selected_tools, list):
            tool_data = {
                "profile": "기본정보",
                "leave_balance": "연차",
            }
            requested_tools = ["profile", *(name for name in selected_tools if name != "profile")]
            data = {
                tool_data[name]: record[tool_data[name]]
                for name in requested_tools
                if name in tool_data and tool_data[name] in record
            }
            used = [
                name
                for name in requested_tools
                if name in tool_data and tool_data[name] in record
            ]
        else:
            data = record
            used = list(data)
    return {"lookup": data, "tools_used": used}


# ───────────── admin_lookup: 인사팀 집계 조회 ─────────────
def admin_lookup(s: HRState) -> HRState:
    """역할은 토큰에서 온 s['role']만 사용. DB가 있으면 app/admin_tools.py의 정해진 집계만 실행"""
    tool, params = s["admin_tool"], s.get("admin_params", {})
    if not config.USE_DB:
        result = {"기준": {}, "건수": 0, "표": [], "안내": "DB가 연결되지 않아 집계 결과가 없어요."}
    else:
        from .. import admin_tools
        try:
            result = admin_tools.run_admin_tool(s.get("role", ""), tool, params)
        except PermissionError as exc:
            result = {"기준": {}, "건수": 0, "표": [], "안내": str(exc)}
        except Exception as exc:
            log.warning("집계 조회 실패 %s: %s", tool, type(exc).__name__)
            result = {"기준": {}, "건수": 0, "표": [], "안내": "집계 조회 중 오류가 났어요. 잠시 뒤 다시 시도해 주세요."}
    return {"admin_result": result, "tools_used": [f"admin:{tool}"]}


def _admin_table(r: dict, max_rows: int = 10) -> str:
    """집계 결과를 사람이 읽는 목록으로 (값은 DB 그대로)"""
    if r.get("안내"):
        return r["안내"]
    rows = r.get("표", [])
    cond = ", ".join(f"{k} {v}" for k, v in r.get("기준", {}).items())
    head = f"조회 결과 {r.get('건수', len(rows))}건이에요." + (f" ({cond})" if cond else "")
    if not rows:
        return head
    def cell(k, v):   # 사용률·이수율·변화율은 %로
        return f"{k} {v * 100:.1f}%" if k.endswith(("률", "율")) and isinstance(v, float) else f"{k} {v}"
    lines = [head] + ["- " + " · ".join(cell(k, v) for k, v in row.items()) for row in rows[:max_rows]]
    if r.get("부서별"):
        lines.append("부서별: " + ", ".join(f"{k} {v}명" for k, v in r["부서별"].items()))
    if "명단" in r:      # 표는 부서별, 건수는 명단 인원이라 남은 줄 수를 따로 셈
        if len(rows) > max_rows:
            lines.append(f"…외 {len(rows) - max_rows}개 부서")
    elif r.get("건수", len(rows)) > max_rows:
        lines.append(f"…외 {r.get('건수', len(rows)) - max_rows}건")
    if r.get("단계"):
        lines.append("촉진 단계: " + " · ".join(f"{k} {v}" for k, v in r["단계"].items()))
    if r.get("주의"):
        lines.append(f"참고: {r['주의']}")
    return "\n".join(lines)


# ───────────── retrieve: 규정 검색 ─────────────
def retrieve(s: HRState) -> HRState:
    """TODO 교체: 이재용 님 리트리버 (BGE-M3 + BM25 → RRF → bge-reranker-v2-m3)"""
    wide = s.get("retries", 0) > 0
    return {"chunks": retriever.search(s["masked_question"], k=5, wide=wide)}


# ───────────── calc: 계산 Tool ─────────────
_DATE_IN_QUESTION = re.compile(
    r"(?<!\d)(?P<year>\d{4})(?:[./-](?P<month>\d{1,2})[./-](?P<day>\d{1,2})"
    r"|년\s*(?P<kmonth>\d{1,2})\s*월\s*(?P<kday>\d{1,2})\s*일)(?!\d)"
)
_MONEY_IN_QUESTION = r"(?P<amount>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(?P<unit>만원|원)"
_HOUR_VALUE = r"(\d+(?:\.\d+)?)\s*시간"
_OVERTIME_HOURS = re.compile(
    rf"(?:시간외(?:근무)?(?:\s*\(연장\))?|연장(?:근무)?)\s*"
    rf"(?:시간(?:은|이|가)?\s*)?{_HOUR_VALUE}"
)
_NIGHT_HOURS = re.compile(rf"야간(?:근무)?\s*(?:시간(?:은|이|가)?\s*)?{_HOUR_VALUE}")
_HOLIDAY_HOURS = re.compile(rf"휴일(?:근무)?\s*(?:시간(?:은|이|가)?\s*)?{_HOUR_VALUE}")


def _question_dates(question: str) -> list[date] | None:
    dates = []
    for match in _DATE_IN_QUESTION.finditer(question):
        month = match.group("month") or match.group("kmonth")
        day = match.group("day") or match.group("kday")
        try:
            dates.append(date(int(match.group("year")), int(month), int(day)))
        except ValueError:
            return None
    return dates


def _labeled_amount(question: str, label: str) -> tuple[str, int | None]:
    patterns = (
        re.compile(rf"{re.escape(label)}\s*(?:은|이|가|으로|:)?\s*{_MONEY_IN_QUESTION}"),
        re.compile(rf"{_MONEY_IN_QUESTION}\s*(?:의)?\s*{re.escape(label)}"),
    )
    matches = []
    for pattern in patterns:
        for match in pattern.finditer(question):
            number = Decimal(match.group("amount").replace(",", ""))
            if match.group("unit") == "만원":
                number *= Decimal(10000)
            if number != number.to_integral_value():
                return "invalid", None
            matches.append(int(number))
    if len(matches) > 1:
        return "ambiguous", None
    if not matches:
        return "missing", None
    return "found", matches[0]


def _hours(question: str) -> tuple[float, float, float] | None:
    matches = (
        _OVERTIME_HOURS.findall(question),
        _NIGHT_HOURS.findall(question),
        _HOLIDAY_HOURS.findall(question),
    )
    if len(matches[0]) != 1 or any(len(values) > 1 for values in matches):
        return None
    try:
        return (
            float(matches[0][0]),
            float(matches[1][0]) if matches[1] else 0.0,
            float(matches[2][0]) if matches[2] else 0.0,
        )
    except ValueError:
        return None


def _calc_from_classifier(calc: dict, chunks: list, question: str) -> dict | None:
    kind = calc["type"]
    slots = calc["slots"]
    if kind == "business_trip":
        if not {"nights", "days", "region"} <= set(slots):
            return None
        nights, days = slots["nights"], slots["days"]
        for chunk in chunks:
            rates = chunk.get("rates")
            if rates:
                daily_allowance = rates["daily_allowance"] * days
                meal_allowance = rates["meal_allowance"] * days
                daily_total = daily_allowance + meal_allowance
                lodging_total = rates["lodging_cap"] * nights
                return {
                    "daily_allowance": daily_allowance,
                    "meal_allowance": meal_allowance,
                    "daily_allowance_and_meal": daily_total,
                    "lodging_cap": lodging_total,
                    "maximum_total": daily_total + lodging_total,
                }
        return None

    if kind == "annual_leave" and "service_years" in slots:
        return {
            "입력 부족": {
                "필요한 값": "입사일(연·월·일)",
                "안내": "연차는 회계연도 기준이라 입사 연도와 날짜가 있어야 계산할 수 있어요.",
            }
        }

    rules = calc_rules.load_rules(config.RULES_PATH)
    if rules is None:
        return None
    if kind == "annual_leave":
        hire_date = slots.get("hire_date")
        as_of = slots.get("as_of")
        if not hire_date or not as_of:
            return None
        return calc_rules.annual_leave(hire_date, as_of, rules)
    if kind == "overtime":
        if (
            "ordinary_wage" in slots
            and "통상임금" not in question
            and any(label in question for label in ("기본급", "월급"))
        ):
            slots = dict(slots)
            slots["base_pay"] = slots.pop("ordinary_wage")
        hour_slots = ("overtime_hours", "night_hours", "holiday_hours")
        if "ordinary_wage" in slots:
            if not any(key in slots for key in hour_slots):
                return None
            return calc_rules.overtime_pay(
                slots["ordinary_wage"],
                slots.get("overtime_hours", 0),
                slots.get("night_hours", 0),
                slots.get("holiday_hours", 0),
                rules,
            )
        if "base_pay" in slots and any(key in slots for key in hour_slots):
            excess = calc_rules.holiday_hours_excess(
                slots.get("holiday_hours", 0), rules
            )
            if excess is not None:
                return excess
            ordinary = rules.get("ordinary_wage", {})
            overtime = rules.get("overtime", {})
            if (
                isinstance(ordinary, dict)
                and isinstance(ordinary.get("_status"), str)
                and isinstance(overtime, dict)
                and "monthly_hours" in overtime
            ):
                return {
                    "입력 부족": {
                        "필요한 값": "통상임금",
                        "통상임금 구성": ordinary["_status"],
                        "시간급 계산식": (
                            f"통상임금 ÷ 월 소정근로시간 ({overtime['monthly_hours']}시간)"
                        ),
                    }
                }
        return None
    if kind == "family_allowance":
        if not {"grade", "family"} <= set(slots):
            return None
        return calc_rules.family_allowance(slots["grade"], slots["family"], rules)
    return None


def calc(s: HRState) -> HRState:
    """출장비: 근거에 단가(rates)가 있으면 박·일수로 계산. TODO: 기준표(rules.json) 기반 계산 함수로 확장 (수당, 경조휴가 등)"""
    question = s["masked_question"]
    classifier_calc = s.get("classifier_calc")
    if config.CALC_TOOLS and isinstance(classifier_calc, dict):
        result = _calc_from_classifier(
            classifier_calc, s.get("chunks", []), question
        )
        if result is not None:
            return {"calc": result}
    trip = re.search(r"(\d+)\s*박\s*(\d+)\s*일", question)
    if "출장" in question and trip:
        nights, days = (int(value) for value in trip.groups())
        for chunk in s.get("chunks", []):
            rates = chunk.get("rates")
            if rates:
                daily_allowance = rates["daily_allowance"] * days
                meal_allowance = rates["meal_allowance"] * days
                daily_total = daily_allowance + meal_allowance
                lodging_total = rates["lodging_cap"] * nights
                return {"calc": {
                    "daily_allowance": daily_allowance,
                    "meal_allowance": meal_allowance,
                    "daily_allowance_and_meal": daily_total,
                    "lodging_cap": lodging_total,
                    "maximum_total": daily_total + lodging_total,
                }}
    if not config.CALC_TOOLS:
        return {}
    rules = calc_rules.load_rules(config.RULES_PATH)
    if rules is None:
        return {}

    calculations = []
    if "연차" in question and "입사" in question:
        dates = _question_dates(question)
        if dates is not None and len(dates) in (1, 2):
            hire_date = dates[0]
            reference_date = dates[1] if len(dates) == 2 else date.today()
            result = calc_rules.annual_leave(hire_date, reference_date, rules)
            if result is not None:
                calculations.append(result)

    if any(term in question for term in ("시간외", "연장", "야간", "휴일")):
        hours = _hours(question)
        if hours is not None:
            ordinary_status, ordinary_wage = _labeled_amount(question, "통상임금")
            basic_status, _ = _labeled_amount(question, "기본급")
            if ordinary_status == "found" and basic_status == "missing":
                result = calc_rules.overtime_pay(
                    ordinary_wage, *hours, rules=rules
                )
                if result is not None:
                    calculations.append(result)
            elif ordinary_status == "missing" and basic_status == "found":
                excess = calc_rules.holiday_hours_excess(hours[2], rules)
                if excess is not None:
                    calculations.append(excess)
                else:
                    ordinary = rules.get("ordinary_wage", {})
                    overtime = rules.get("overtime", {})
                    if (
                        isinstance(ordinary, dict)
                        and isinstance(ordinary.get("_status"), str)
                        and isinstance(overtime, dict)
                        and "monthly_hours" in overtime
                    ):
                        calculations.append({
                            "입력 부족": {
                                "필요한 값": "통상임금",
                                "통상임금 구성": ordinary["_status"],
                                "시간급 계산식": (
                                    f"통상임금 ÷ 월 소정근로시간 ({overtime['monthly_hours']}시간)"
                                ),
                            }
                        })
    if len(calculations) == 1:
        return {"calc": calculations[0]}
    return {}


# ───────────── generate: 답변 생성 ─────────────
SYSTEM_PROMPT = """당신은 누리철도㈜ 인사(HR) 안내 챗봇입니다. 아래 규칙을 반드시 지키세요.
1. [근거]와 [조회 결과], [계산 결과]에 있는 내용으로만 답하세요. 근거에 없는 내용, 금액, 일수는 지어내지 마세요.
2. 근거에서 답을 찾을 수 없으면 "규정에서 확인되지 않아요. 인사팀에 확인해 주세요."라고 답하세요.
3. 질문의 전제가 근거와 다르면 먼저 "아니요"라고 바로잡고 근거의 내용을 알려 주세요.
4. [조회 결과]의 조건(근속, 직급, 가족, 나이)을 근거의 기준에 하나씩 대입해 답하세요.
5. 받을 수 있는 것이 여러 개면 휴가·금액·지원·신청 기한을 빠짐없이 항목별로 쓰세요.
6. 금액이나 일수를 계산할 때는 계산식과 결과를 함께 쓰세요.
7. '각'으로 표시된 금액은 항목마다 따로 계산한 뒤 합산하세요. [계산 결과]가 있으면 다시 계산하지 말고 그 값을 그대로 쓰세요.
8. 근거에 [예시 설명]이 있으면 그 금액 산출 결과를 답변의 기준으로 따르세요.
9. 한국어로 간결하게 쓰고, 숫자(일수·금액·비율)는 근거·조회 결과와 똑같이 쓰세요. 조문 번호는 답 끝에 괄호로 적으세요."""


def _won(n):
    return f"{int(n):,}원"


def _lookup_sentences(lk: dict) -> list[str]:
    """조회 결과를 문장으로 (가짜 LLM용). 값은 조회 결과 그대로"""
    out = []
    if "연차" in lk:
        a = lk["연차"]
        out.append(f"{a['연도']}년 연차는 {a['부여일수']}일 중 {a['사용일수']:g}일을 써서 {a['잔여일수']:g}일 남았어요. ({a['기준일']} 기준)")
    if "복지포인트" in lk:
        w = lk["복지포인트"]
        out.append(f"{w['연도']}년 복지포인트는 {w['부여포인트']:,}점 중 {w['사용포인트']:,}점을 써서 {w['잔여포인트']:,}점 남았어요. {w['만료일']}까지 쓸 수 있어요.")
    if "급여" in lk:
        p = lk["급여"]
        out.append(f"{p['급여월']} 급여는 지급총액 {_won(p['지급총액'])}, 실수령액 {_won(p['실수령액'])}이에요. "
                   f"시간외근무 {p['시간외근무시간']:g}시간에 시간외수당 {_won(p['시간외수당'])}, 가족수당 {_won(p['가족수당'])}이 들어 있어요. (지급일 {p['지급일']})")
    if "가족" in lk:
        f = lk["가족"]
        out.append("등록된 가족은 " + ", ".join(f"{x['관계']}({x['나이']}세{', 부양' if x['부양가족'] else ''})" for x in f["가족"])
                   + f"이고, 부양가족은 {f['부양가족수']}명이에요.")
    if "신청현황" in lk:
        out.append("최근 신청은 " + ", ".join(f"{r['신청일']} {r['종류']}({r['내용']}) {r['상태']}" for r in lk["신청현황"]["최근신청"][:3]) + "이에요.")
    if "교육" in lk:
        t = lk["교육"]
        out.append(("아직 이수하지 않은 필수교육은 " + ", ".join(t["필수교육_미이수"]) + "이에요.") if t["필수교육_미이수"]
                   else f"필수교육은 모두 이수했어요. (전체 {t['전체과정수']}개 과정 중 {t['이수과정수']}개 이수)")
    return out


def _fake_answer(s: HRState) -> str:
    lk, chunks = s.get("lookup", {}), s.get("chunks", [])
    if s.get("admin_result") is not None:         # 인사팀: 표는 generate가 붙이므로 여기선 근거만
        if s.get("intent") == "data" or not chunks:
            return "집계 결과를 정리했어요."
        top = chunks[0]
        return top.get("canned") or f"[가짜 LLM] 가장 가까운 근거는 '{top['citation']}'이에요: {top['text'][:150]}"
    parts = _lookup_sentences(lk) if s.get("intent") in ("lookup", "both") else []
    if s.get("intent") == "lookup" and not parts and lk.get("기본정보"):
        b = lk["기본정보"]
        parts.append(f"{b.get('부서', '')} {b.get('직위', '')}({b.get('직급', '')}), 입사일 {b.get('입사일', '')}, 근속 {b.get('근속연수', '')}년이에요.")
    if chunks and s.get("intent") != "lookup":
        top = chunks[0]
        parts.append(top.get("canned") or f"[가짜 LLM] 가장 가까운 근거는 '{top['citation']}'이에요: {top['text'][:150]}")
    return " ".join(parts)


def _clean(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)     # 생각 과정이 섞여 나오면 제거
    return text.split("</think>")[-1].strip()


ADMIN_PROMPT_ROWS = 30   # 집계 표를 LLM에 넘길 최대 행 수 (전체 표는 답변 뒤에 코드가 붙임)


def _prompt_lookup(s: HRState) -> dict:
    r = s.get("admin_result")
    if r is None:
        return s.get("lookup", {})
    return {"인사팀 집계": {**r, "표": r.get("표", [])[:ADMIN_PROMPT_ROWS]},
            "안내": "표의 숫자를 요약만 하고 명단을 다시 나열하지 마세요. 표는 답변 뒤에 따로 붙어요."}


def _llm_answer(s: HRState) -> str:
    ctx = "\n\n".join(
        f"[{c['citation']}]\n{c['text']}"
        + (f"\n[예시 설명]\n{c['canned']}" if c.get("canned") else "")
        for c in s.get("chunks", [])[:5]
    )
    user_prompt = (
        f"[근거]\n{ctx or '(없음)'}\n\n"
        f"[조회 결과]\n{json.dumps(_prompt_lookup(s), ensure_ascii=False, default=str)}\n\n"
        f"[계산 결과]\n{json.dumps(s.get('calc', {}), ensure_ascii=False)}\n\n"
        f"[질문]\n{s['masked_question']}"
    )
    base = urlsplit(config.LLM_BASE_URL)
    if base.scheme == "http" and base.hostname in {"localhost", "127.0.0.1"} \
            and base.port == 11434 and base.path.rstrip("/") == "/v1":
        # Ollama의 OpenAI 호환 주소는 이 모델에 think=false가 적용되지 않아서, Ollama 고유 주소(/api/chat)로 호출
        response = httpx.post(
            f"{base.scheme}://{base.netloc}/api/chat",
            json={
                "model": config.LLM_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "think": False,
                "options": {
                    "temperature": 0,
                    "num_predict": config.LLM_MAX_TOKENS,
                },
            },
            timeout=config.LLM_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("done_reason") == "length":
            log.warning(
                "Ollama 답변이 LLM_MAX_TOKENS=%d 에서 잘렸습니다.",
                config.LLM_MAX_TOKENS,
            )
        return _clean(data["message"]["content"])

    from langchain_core.prompts import ChatPromptTemplate
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY, model=config.LLM_MODEL,
                     temperature=0, timeout=config.LLM_TIMEOUT, max_retries=1,
                     max_tokens=config.LLM_MAX_TOKENS)
    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        ("human", "{user_prompt}"),
    ])
    msg = (prompt | llm).invoke({"user_prompt": user_prompt})
    return _clean(msg.content)


def generate(s: HRState) -> HRState:
    answer = _fake_answer(s) if config.USE_FAKE_LLM else _llm_answer(s)
    if s.get("admin_result") is not None:          # 인사팀 집계 표는 LLM이 아니라 코드가 그대로 붙임
        answer = f"{answer}\n\n{_admin_table(s['admin_result'])}".strip()
    return {"answer": answer}


def normalize_no_evidence(s: HRState) -> HRState:
    if s.get("intent") == "lookup":
        return {}
    if not s.get("answer", "").startswith("규정에서 확인되지 않아요"):
        return {}
    return {
        "answer": "규정에서 확인되지 않아요. 인사팀에 확인해 주세요.",
        "kind": "refused",
        "chunks": [],
    }


def refuse_no_question(s: HRState) -> HRState:
    return {
        "answer": "어떤 내용이 궁금한지 알려 주세요. 개인정보는 적지 않아도 돼요.",
        "kind": "refused",
        "chunks": [],
        "tools_used": [],
    }


def out_of_scope(s: HRState) -> HRState:
    return {
        "answer": "저는 회사 인사 규정과 본인 인사 정보만 안내해요. 휴가·급여·복지·출장 같은 인사 관련 질문을 해 주세요.",
        "kind": "refused",
        "chunks": [],
        "tools_used": [],
    }


def mask_response(s: HRState) -> HRState:
    answer = s.get("answer", "")
    public_answer = mask_answer(answer).text
    answer_masked = None
    if not s.get("tools_used"):
        answer_masked = mask_answer(
            answer,
            own_emp_id=s["employee_id"],
            own_name=s.get("own_name"),
        ).text
    return {"answer": public_answer, "answer_masked": answer_masked}


# ───────────── verify: 근거 검증 ─────────────
def verify(s: HRState) -> HRState:
    """TODO 교체: 답변 숫자 ↔ 근거·계산 숫자 대조 + 리랭커 점수"""
    if s.get("intent") == "lookup":
        return {"verified": bool(s.get("lookup")) and bool(s.get("answer"))}
    if s.get("intent") == "data":
        return {"verified": s.get("admin_result") is not None and bool(s.get("answer"))}
    chunks = s.get("chunks", [])
    ok = bool(chunks) and chunks[0]["score"] >= VERIFY_MIN_SCORE and bool(s.get("answer"))
    return {"verified": ok, "retries": s.get("retries", 0) + (0 if ok else 1)}


def route_after_verify(s: HRState) -> str:
    if s.get("verified"):
        return "log"
    return "retrieve" if s.get("retries", 0) <= MAX_RETRIES else "refuse"


# ───────────── refuse / sensitive ─────────────
def refuse(s: HRState) -> HRState:
    if s.get("intent") == "refuse" and s.get("role") == "hr_admin":
        msg = "주민번호·계좌·주소·연락처는 챗봇으로 조회할 수 없어요. 인사시스템에서 권한 절차를 거쳐 확인해 주세요."
    elif s.get("intent") == "refuse":
        msg = "다른 직원의 정보나 권한 밖의 내용은 알려드릴 수 없어요. 필요하면 인사팀에 정식으로 요청해 주세요."
    elif s.get("intent") == "unavailable":
        msg = "이 데이터는 지금 챗봇이 조회할 수 있는 DB에 없어요. 담당 시스템(근태·인사시스템)에서 확인해 주세요."
    else:
        msg = "이 질문은 등록된 규정에서 근거를 찾지 못했어요. 정확한 내용은 인사팀에 확인해 주세요."
    return {"answer": msg, "kind": "refused", "chunks": []}


def sensitive(s: HRState) -> HRState:
    return {
        "answer": ("이 주제는 챗봇이 판단하지 않아요. 고충처리 담당자나 인사팀 상담 창구로 직접 상담해 주세요. "
                   "신고 내용은 비밀이 보장되는 절차로 처리돼요."),
        "kind": "sensitive",
        "chunks": [],
    }


# ───────────── log: 감사 로그 ─────────────
def write_log(s: HRState) -> HRState:
    """가명 ID와 마스킹한 내용만 콘솔·DB 로그에 저장한다."""
    kind = s.get("kind") or "answer"
    hide = s.get("intent") == "sensitive" or s.get("sensitive_topic")   # sensitive_topic: 인사팀의 민감 주제 질문
    question_masked = "[민감 주제 문의]" if hide else s["masked_question"]
    pii_found = dict(s.get("pii_found", {}))
    if s.get("intent") == "refuse":
        def mask_other_name(match: re.Match) -> str:
            if re.fullmatch(_TITLE, match.group("name")):
                return match.group(0)
            pii_found["타인이름"] = pii_found.get("타인이름", 0) + 1
            return "[타인이름]" + match.group("title")

        question_masked = _LOG_OTHER_NAME.sub(mask_other_name, question_masked)
    if hide:
        answer_masked = None
    elif s.get("admin_result") is not None:
        answer_masked = ADMIN_ANSWER_LOG_PLACEHOLDER if s.get("answer") else None
    elif s.get("tools_used"):
        answer_masked = LOOKUP_ANSWER_LOG_PLACEHOLDER if s.get("answer") else None
    else:
        answer_masked = s.get("answer_masked")
    payload = {
        "user_pseudo_id": (
            SENSITIVE_PSEUDO_LOG_PLACEHOLDER
            if s.get("intent") == "sensitive" and config.SENSITIVE_LOG_PSEUDO == "drop"
            else pseudonymize(s["employee_id"])
        ),
        "question_masked": question_masked,
        "answer_masked": answer_masked,
        "pii_found": pii_found,
        "intent": s.get("intent"),
        "kind": kind,
        "cited_chunks": [c["id"] for c in s.get("chunks", [])[:3] if c.get("id")],
        "tools_used": s.get("tools_used", []),
        "latency_ms": s.get("latency_ms"),
    }
    log.info(json.dumps(payload, ensure_ascii=False))
    if config.USE_DB and config.CHAT_LOG_DB:
        try:
            from .. import db
            db.insert_chat_log(**payload)
        except Exception as exc:
            log.warning("DB 대화 로그 저장 실패: %s", type(exc).__name__)
    return {"kind": kind}
