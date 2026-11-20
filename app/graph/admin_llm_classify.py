"""인사팀(hr_admin) 질문의 LLM 분류.

임직원 LLM 분류(llm_classify)와 같은 의도·본인 조회 도구·계산 값에 더해, 집계 도구를 쓸지와 그 인자까지 골라요.
여기서 도구 이름·인자 이름·값 형식을 다시 확인하고, 틀린 값은 버려요. 응답 전체가 이상하면 None을 돌려줘서
규칙 결과를 쓰게 해요.
"""
import inspect
import json
import logging
from datetime import date

import httpx

from .. import admin_tools, config
from . import llm_classify

log = logging.getLogger("hr_chat")

# 인사팀은 다른 직원을 볼 권한이 있어서 refuse가 없고, 민감 주제는 의도가 아니라 표시(sensitive_topic)로 다뤄요.
INTENTS = ("rule", "lookup", "both", "out_of_scope")
OUTPUT_LIMIT = 512

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "tools": {"type": "array", "items": {"type": "string", "enum": list(llm_classify.TOOL_NAMES)}},
        "calc": llm_classify._CALC_SCHEMA,
        "admin_tool": {"anyOf": [{"type": "null"}, {"type": "string", "enum": list(admin_tools.ADMIN_TOOLS)}]},
        "admin_params": {"type": "object", "additionalProperties": True},
    },
    "required": ["intent", "tools", "calc", "admin_tool", "admin_params"],
    "additionalProperties": False,
}


def _answer(intent, tools=(), calc=None, admin_tool=None, admin_params=None) -> dict:
    return {"intent": intent, "tools": list(tools), "calc": calc,
            "admin_tool": admin_tool, "admin_params": admin_params or {}}


# 예시의 오늘은 2026-11-17(월요일)이에요.
_EXAMPLE_TODAY = "2026-11-17"
EXAMPLES = [
    ("올해 연차 사용률 50% 안 되는 부서 어디야?",
     _answer("both", admin_tool="dept_leave_usage", admin_params={"year": 2026, "below_rate": 0.5})),
    ("올해 휴가 제일 많이 남은 사람 20명만",
     _answer("both", admin_tool="leave_remaining_list", admin_params={"year": 2026, "limit": 20})),
    ("요즘 회사 그만둔 사람 어느 팀에서 많이 나갔어?",
     _answer("both", admin_tool="retirements_by_dept", admin_params={"start": "2026-01-01", "end": "2026-11-17"})),
    ("김민지 과장 이번 달 휴가 쓴 날짜",
     _answer("both", admin_tool="leave_people_period",
             admin_params={"start": "2026-11-01", "end": "2026-11-30", "leave_type": "연차"})),
    ("아직 결재 안 올라간 출장 건 뭐 있어?",
     _answer("both", admin_tool="pending_requests", admin_params={"request_type": "출장신청"})),
    ("내 연차 몇 개 남았어?", _answer("lookup", tools=["profile", "leave_balance"])),
    ("제 연차로 다음 달 결혼휴가 붙여 쓸 수 있어?", _answer("both", tools=["profile", "leave_balance"])),
    ("입사일이 2019-03-02인 직원 올해 연차 며칠이야?",
     _answer("rule", calc={"type": "annual_leave", "slots": {"hire_date": "2019-03-02", "as_of": "2026-11-17"}})),
    ("출산휴가는 며칠까지 줄 수 있어?", _answer("rule")),
    ("징계 감경 사유가 뭐가 있어?", _answer("rule")),
    ("이번 주말 날씨 어때?", _answer("out_of_scope")),
]


def _param_kind(fn, name: str) -> str:
    """인자 종류: date / int / float / str (타입 표시로 판단)"""
    annotation = str(inspect.signature(fn).parameters[name].annotation)
    for kind in ("date", "float", "int", "str"):
        if kind in annotation:
            return kind
    return "str"


def _catalog_text() -> str:
    lines = []
    for name, fn in admin_tools.ADMIN_TOOLS.items():
        doc = (fn.__doc__ or "").strip().splitlines()
        params = ", ".join(f"{p}:{_param_kind(fn, p)}" for p in inspect.signature(fn).parameters)
        lines.append(f"- {name}({params}): {doc[0] if doc else ''}")
    return "\n".join(lines)


def _system_prompt(today: date) -> str:
    examples = "\n".join(
        f"{i}. 질문: {q}\n정답 JSON: {json.dumps(a, ensure_ascii=False, separators=(',', ':'))}"
        for i, (q, a) in enumerate(EXAMPLES, 1)
    )
    return f"""오늘 날짜: {today.isoformat()}
당신은 인사팀 담당자의 질문을 분류합니다. 인사팀은 업무상 다른 직원의 인사 데이터를 조회할 권한이 있으므로,
다른 직원이나 특정 직원 이름이 나온다는 이유로 거절하지 않습니다. 징계·괴롭힘·고충도 인사팀에게는 업무 주제입니다.
의도:
- rule: 규정·절차·기준만으로 답함.
- lookup: 질문한 담당자 본인의 저장 데이터만 확인함 (내 연차, 내 급여 등).
- both: 데이터와 규정 근거가 모두 필요함. 여러 직원의 집계·명단이 필요하면 admin_tool을 고르고,
  담당자 본인 데이터가 필요하면 tools를 고릅니다.
- out_of_scope: 인사 업무와 무관함 (날씨, 잡담, 재테크, 사내 IT·총무 등).
본인 조회 도구 (tools, lookup·both에서 본인 데이터가 필요할 때만): {", ".join(llm_classify.TOOL_NAMES)}. 본인 조회면 profile을 포함합니다.
집계 도구 (admin_tool, 여러 직원 데이터가 필요할 때만 하나. 이름(인자:형식): 설명):
{_catalog_text()}
계산 슬롯 (calc, 질문에 계산할 값이 있을 때만):
annual_leave(hire_date, as_of) 또는 service_years; overtime(ordinary_wage, base_pay, overtime_hours, night_hours, holiday_hours);
family_allowance(grade, family[관계, 나이, 성별, 부양]); business_trip(nights, days, region).
규칙:
- admin_tool을 고르면 intent는 both입니다. 집계가 필요 없으면 admin_tool은 null입니다.
- admin_params와 calc에는 질문에 나타난 값만 넣습니다. 없으면 비워 둡니다 (도구 기본값을 씀).
- 날짜는 YYYY-MM-DD, 월은 YYYY-MM, 비율은 0~1 소수(30% → 0.3)로 씁니다. 상대 기간은 오늘 기준으로 계산합니다.
예시 (예시의 오늘은 {_EXAMPLE_TODAY}):
{examples}
출력은 지정된 JSON 스키마만 따르세요."""


def _request(question: str, today: date) -> str:
    response = httpx.post(
        llm_classify._chat_url(),
        json={
            "model": config.CLASSIFY_MODEL,
            "messages": [
                {"role": "system", "content": _system_prompt(today)},
                {"role": "user", "content": question},
            ],
            "format": OUTPUT_SCHEMA,
            "stream": False,
            "think": False,
            "options": {"temperature": 0, "num_predict": OUTPUT_LIMIT},
        },
        timeout=config.CLASSIFY_TIMEOUT,
    )
    response.raise_for_status()
    content = response.json()["message"]["content"]
    if not isinstance(content, str):
        raise TypeError("Ollama response content is not text")
    return content


def _coerce(kind: str, value):
    """값을 인자 형식으로 바꿔요. 바꿀 수 없으면 None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if kind == "date":
            return date.fromisoformat(value) if isinstance(value, str) else None
        if kind == "int":
            number = float(value)
            return int(number) if number.is_integer() and number >= 0 else None
        if kind == "float":
            number = float(value)
            return number if number >= 0 else None
        return (value.strip() or None) if isinstance(value, str) else None
    except (TypeError, ValueError):
        return None


def _validate_admin_params(tool: str, params) -> dict:
    if not isinstance(params, dict):
        return {}
    fn = admin_tools.ADMIN_TOOLS[tool]
    allowed = inspect.signature(fn).parameters
    cleaned = {}
    for key, value in params.items():
        if key not in allowed:
            continue
        if key in ("month", "pay_month"):          # "YYYY-MM" 문자열 인자
            ok = isinstance(value, str) and len(value) == 7 and value[4] == "-"
            coerced = value if ok and value[:4].isdigit() and value[5:].isdigit() else None
        else:
            coerced = _coerce(_param_kind(fn, key), value)
        if coerced is None:
            log.warning("인사팀 LLM 분류 응답에서 형식이 맞지 않는 인자를 제외했습니다")
            continue
        cleaned[key] = coerced
    if "start" in cleaned and "end" in cleaned and cleaned["end"] < cleaned["start"]:
        cleaned.pop("start"), cleaned.pop("end")
    return cleaned


def _validate_tools(tools) -> list[str] | None:
    if not isinstance(tools, list):
        return None
    selected = []
    for name in tools:
        if name in llm_classify.TOOL_NAMES and name not in selected:
            selected.append(name)
        else:
            log.warning("인사팀 LLM 분류 응답에서 허용되지 않은 조회 도구를 제외했습니다")
    return selected


def _validate_response(raw: str, today: date) -> dict | None:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("intent") not in INTENTS:
        return None
    intent = payload["intent"]
    result = {
        "intent": intent,
        "tools": _validate_tools(payload.get("tools")),
        "calc": llm_classify._validate_calc(payload.get("calc"), today),
        "admin_tool": None,
        "admin_params": {},
    }
    tool = payload.get("admin_tool")
    if tool in admin_tools.ADMIN_TOOLS and intent in ("both", "rule", "lookup"):
        result.update(intent="both", admin_tool=tool,
                      admin_params=_validate_admin_params(tool, payload.get("admin_params")))
    elif tool is not None and tool not in admin_tools.ADMIN_TOOLS:
        log.warning("인사팀 LLM 분류 응답에서 모르는 집계 도구를 제외했습니다")
    if result["intent"] in ("lookup", "both") and not result["admin_tool"] and result["tools"] is not None:
        if "profile" not in result["tools"]:
            result["tools"].insert(0, "profile")
    if result["intent"] == "out_of_scope":
        result.update(tools=[], calc=None)
    return result


def classify(question: str, today: date) -> dict | None:
    """인사팀 질문 → {intent, tools, calc, admin_tool, admin_params}. 요청·응답이 잘못되면 None."""
    if not isinstance(question, str) or not isinstance(today, date):
        return None
    try:
        raw = _request(question, today)
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        log.warning("인사팀 LLM 분류 요청 실패: %s", type(exc).__name__)
        return None
    return _validate_response(raw, today)
