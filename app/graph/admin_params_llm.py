"""③ 인사팀 분류 보조 LLM: 규칙이 고른 집계 도구의 기간·기준값만 다시 뽑아요.

도구 선택은 규칙이 해요 (골든셋에서 규칙 도구 정확도가 LLM보다 높았음). 규칙의 값 추출은 표현이 다르면 놓쳐서
("요즘", "작년 하반기", "20명만") 그 도구의 인자만 LLM에 묻고, 형식을 다시 확인해요.
규칙이 비워 둔(None·없음) 인자만 LLM 값으로 채우고, 규칙이 뽑은 값은 덮어쓰지 않아요 (admin_classify.classify_admin_state).
요청·응답이 잘못되면 None을 돌려줘서 규칙 값을 그대로 쓰게 해요.
"""
import inspect
import json
import logging
import re
from datetime import date

import httpx

from .. import admin_tools
from . import llm_classify

log = logging.getLogger("hr_chat")

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"params": {"type": "object", "additionalProperties": True}},
    "required": ["params"],
    "additionalProperties": False,
}

# 예시의 오늘은 2026-11-17(월요일)이에요.
_EXAMPLE_TODAY = "2026-11-17"
_EXAMPLES = [
    ("올해 연차 사용률 50% 안 되는 부서 어디야?", "dept_leave_usage", {"year": 2026, "below_rate": 0.5}),
    ("올해 휴가 제일 많이 남은 사람 20명만", "leave_remaining_list", {"year": 2026, "limit": 20}),
    ("요즘 회사 그만둔 사람 어느 팀에서 많이 나갔어?", "retirements_by_dept", {"start": "2026-01-01", "end": "2026-11-17"}),
    ("김민지 과장 이번 달 휴가 쓴 날짜", "leave_people_period",
     {"start": "2026-11-01", "end": "2026-11-30", "leave_type": "연차"}),
    ("아직 결재 안 올라간 출장 건 뭐 있어?", "pending_requests", {"request_type": "출장신청"}),
]


_KINDS = ("date", "float", "int", "str")


def _param_kinds(fn, name: str) -> tuple[str | None, str | None]:
    """인자 형식 (값 하나일 때 종류, 목록일 때 항목 종류). 예: str | list[str] → ("str", "str"), list[int] → (None, "int")"""
    annotation = str(inspect.signature(fn).parameters[name].annotation)
    listed = re.search(r"list\[(\w+)", annotation)
    item = next((k for k in _KINDS if listed and k in listed.group(1)), None)
    rest = re.sub(r"list\[[^\]]*\]", "", annotation)
    single = next((k for k in _KINDS if k in rest), None)
    if single is None and item is None:
        single = "str"
    return single, item


def _param_kind(fn, name: str) -> str:
    """프롬프트에 적는 인자 형식: date / int / float / str, 목록이면 [str], 둘 다 되면 'str 또는 [str]'"""
    single, item = _param_kinds(fn, name)
    return " 또는 ".join(k for k in (single, f"[{item}]" if item else None) if k)


def _signature(name: str) -> str:
    fn = admin_tools.ADMIN_TOOLS[name]
    doc = (fn.__doc__ or "").strip().splitlines()
    params = ", ".join(f"{p}:{_param_kind(fn, p)}" for p in inspect.signature(fn).parameters)
    return f"{name}({params}): {doc[0] if doc else ''}"


def _system_prompt(tool: str, today: date) -> str:
    examples = "\n".join(
        f"질문: {q}\n도구: {_signature(t)}\n답: {json.dumps({'params': p}, ensure_ascii=False)}"
        for q, t, p in _EXAMPLES if t in admin_tools.ADMIN_TOOLS
    )
    return f"""오늘 날짜: {today.isoformat()}
인사팀 담당자의 질문에 쓸 집계 도구는 이미 정해졌습니다. 질문에서 이 도구의 인자 값만 뽑으세요.
도구: {_signature(tool)}
규칙:
- 질문에 나타난 값만 넣습니다. 질문에 없는 인자는 넣지 않습니다 (도구 기본값을 씀).
- 형식이 [종류]인 인자는 목록입니다. 질문에 여러 개가 나오면 모두 넣습니다 ("생리휴가·보건휴가" → ["생리휴가", "보건휴가"]).
- 날짜는 YYYY-MM-DD, 월은 YYYY-MM, 비율은 0~1 소수(30% → 0.3)로 씁니다.
- 상대 기간(이번 달, 지난주, 올해, 작년 하반기, 최근 3개월, 요즘)은 오늘 기준으로 계산합니다.
예시 (예시의 오늘은 {_EXAMPLE_TODAY}):
{examples}
출력은 지정된 JSON 스키마만 따르세요."""


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


def validate_params(tool: str, params) -> dict:
    """도구 인자 이름·형식 확인. 모르는 인자와 형식이 틀린 값은 버려요."""
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
            single, item = _param_kinds(fn, key)
            if isinstance(value, list):              # 목록 인자 (휴가 종류 여러 개, 근속 연수 여러 개)
                items = [_coerce(item, v) for v in value] if item else []
                coerced = [v for v in items if v is not None] or None
            else:
                coerced = _coerce(single, value) if single else None
        if coerced is None:
            log.warning("인사팀 인자 LLM 응답에서 형식이 맞지 않는 인자를 제외했습니다")
            continue
        cleaned[key] = coerced
    if "start" in cleaned and "end" in cleaned and cleaned["end"] < cleaned["start"]:
        cleaned.pop("start"), cleaned.pop("end")
    return cleaned


def extract(question: str, tool: str, today: date) -> dict | None:
    """질문 + 규칙이 고른 도구 → 그 도구의 인자 {이름: 값}. 요청·응답이 잘못되면 None."""
    if tool not in admin_tools.ADMIN_TOOLS or not isinstance(question, str):
        return None
    try:
        raw = llm_classify.chat_json(_system_prompt(tool, today), question, OUTPUT_SCHEMA, 128)
        payload = json.loads(raw)
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        log.warning("인사팀 인자 LLM 호출 실패: %s", type(exc).__name__)
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("params"), dict):
        return None
    return validate_params(tool, payload["params"])
