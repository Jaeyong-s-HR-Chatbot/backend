"""LLM-based intent and calculation-slot classification."""
import hashlib
import json
import logging
import math
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from .. import config, tools

log = logging.getLogger("hr_chat")

INTENTS = ("rule", "lookup", "both", "sensitive", "refuse", "out_of_scope")
CALC_TYPES = ("annual_leave", "overtime", "family_allowance", "business_trip")
TOOL_NAMES = ("profile", *(name for _, name, _ in tools.TOOL_KEYWORDS))
OUTPUT_LIMIT = 512
EXAMPLES_PATH = Path(__file__).resolve().parents[1] / "data" / "classify_examples.json"

_CALC_SCHEMA = {
    "anyOf": [
        {"type": "null"},
        {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": list(CALC_TYPES)},
                "slots": {"type": "object", "additionalProperties": True},
            },
            "required": ["type", "slots"],
            "additionalProperties": False,
        },
    ]
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "tools": {"type": "array", "items": {"type": "string", "enum": list(TOOL_NAMES)}},
        "calc": _CALC_SCHEMA,
    },
    "required": ["intent", "tools", "calc"],
    "additionalProperties": False,
}


@dataclass
class EvaluationStats:
    calls: int = 0
    cache_hits: int = 0
    json_failures: int = 0
    validation_failures: int = 0
    errors: list[str] = field(default_factory=list)
    raw_json_failures: list[tuple[str, str]] = field(default_factory=list)
    request_ms: list[float] = field(default_factory=list)


_evaluation_context: ContextVar[tuple[Path, EvaluationStats] | None] = ContextVar(
    "classify_evaluation_context", default=None
)


@contextmanager
def evaluation_cache(path: Path):
    """Enable response caching and request accounting for one evaluation run."""
    stats = EvaluationStats()
    token = _evaluation_context.set((path, stats))
    try:
        yield stats
    finally:
        _evaluation_context.reset(token)


def _examples() -> list[dict]:
    with EXAMPLES_PATH.open(encoding="utf-8") as file:
        data = json.load(file)
    examples = data.get("examples") if isinstance(data, dict) else None
    if not isinstance(examples, list):
        raise ValueError(f"{EXAMPLES_PATH}: examples must be a list")
    return examples


def _system_prompt(today: date) -> str:
    guide_summary = """의도는 질문의 목적에 따라 고릅니다.
- rule: 규정·절차·조건만으로 답함. both: 본인 자료와 규정 근거가 모두 필요함.
- lookup: 로그인한 본인의 저장 데이터만 확인함.
- out_of_scope: 인사 규정·본인 인사 데이터와 관계없음 (날씨, 잡담, 재테크, 사내 IT·총무, 다른 조직의 제도 등).
- sensitive: 직장 내 괴롭힘·성희롱·폭언 등 피해·고충을 알리거나 도움을 구함. "괴롭힘" 같은 단어가 없어도 따돌림·무시·배제·위협·모욕·원치 않는 사적 접촉처럼 피해를 겪고 있다는 호소(예: "저를 일부러 피해요", "의견만 무시해요", "불안하고 힘들어요")면 sensitive.
- refuse: 다른 직원의 개인정보·급여·인사정보 등 권한 밖 자료를 요구함.
- no_question은 호출 전에 처리하므로 출력하지 않습니다.
경계: 일반 제도 설명은 rule, 본인 잔액까지 기준과 대조하면 both. 저장된 신청 상태는 lookup, 앞으로 신청할 방법·서류는 rule.
결혼·출산·사망·이사·육아휴직·퇴직 같은 생애 이벤트도 주제가 아니라 필요한 것으로 나눕니다: 규정만 필요하면 rule, 본인 자료도 필요하면 both.
우리 회사 인사 규정으로 답하는 질문이면 rule, 공무원·다른 회사 제도나 인사와 무관한 회사 일은 out_of_scope입니다.
가족수당 금액·내역은 payroll, 부양가족 등록 자료는 family입니다.
민감 단어만으로 sensitive라 하지 않습니다. 징계 규정이나 "괴롭힘 신고 절차가 어떻게 돼?"처럼 피해 호소 없이 중립적으로 규정·절차만 묻는 것은 rule입니다.
사용 도구: profile, leave_balance, welfare_points, payroll, family, requests, training. 본인 조회가 필요하면 profile을 포함합니다.
계산 슬롯:
annual_leave(hire_date, as_of) 또는 service_years;
overtime(ordinary_wage, base_pay, overtime_hours, night_hours, holiday_hours);
family_allowance(grade, family[관계, 나이, 성별, 부양]);
business_trip(nights, days, region).
질문에 나타난 값만 추출하고, 없는 속성·시간·나이·성별은 채우지 않습니다.
기본급·월급은 ordinary_wage가 아니라 base_pay입니다. service_years만으로 입사일을 만들지 않습니다.
날짜는 YYYY-MM-DD로 정규화합니다. 상대 날짜는 오늘 날짜를 기준으로 계산합니다.
"""
    examples = []
    for example in _examples():
        if example.get("intent") == "no_question":
            examples.append(
                f"사전 종료 예시 (no_question, 분류 호출 대상 아님): {example['question']}"
            )
            continue
        expected = {
            "intent": example["intent"],
            "tools": example["tools"],
            "calc": example["calc"],
        }
        examples.append(
            "질문: "
            + example["question"]
            + "\n정답 JSON: "
            + json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
        )
    return (
        "오늘 날짜: "
        + today.isoformat()
        + "\n"
        + guide_summary
        + "\n예시:\n"
        + "\n".join(f"{index + 1}. {example}" for index, example in enumerate(examples))
        + "\n출력은 지정된 JSON 스키마만 따르세요."
    )


def prompt_hash(today: date) -> str:
    prompt = _system_prompt(today)
    payload = json.dumps(
        {"system": prompt, "schema": OUTPUT_SCHEMA},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_path(question: str, today: date, model: str, cache_dir: Path) -> Path:
    prompt_digest = prompt_hash(today)
    identity = json.dumps(
        [model, prompt_digest, question],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return cache_dir / (hashlib.sha256(identity.encode("utf-8")).hexdigest() + ".json")


def _chat_url() -> str:
    configured = config.LLM_BASE_URL or "http://localhost:11434"
    parts = urlsplit(configured)
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    if not path.endswith("/api/chat"):
        path += "/api/chat"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _request(question: str, today: date) -> str:
    response = httpx.post(
        _chat_url(),
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
    payload = response.json()
    content = payload["message"]["content"]
    if not isinstance(content, str):
        raise TypeError("Ollama response content is not text")
    return content


def _nonnegative_number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _normalize_family(family) -> list[dict] | None:
    if not isinstance(family, list):
        return None
    aliases = {
        "relation": "관계",
        "age": "나이",
        "gender": "성별",
        "is_dependent": "부양",
    }
    allowed = {"관계", "나이", "성별", "부양", *aliases}
    normalized = []
    for member in family:
        if not isinstance(member, dict) or set(member) - allowed:
            return None
        item = {}
        for key, value in member.items():
            target = aliases.get(key, key)
            if target in item:
                return None
            item[target] = value
        if "관계" not in item or not isinstance(item["관계"], str) or not item["관계"].strip():
            return None
        if "나이" in item and (
            not _nonnegative_number(item["나이"])
            or not float(item["나이"]).is_integer()
        ):
            return None
        if "성별" in item and item["성별"] not in {"남", "여", "M", "F"}:
            return None
        if "부양" in item and not isinstance(item["부양"], bool):
            return None
        normalized.append(item)
    return normalized


def _validate_calc(calc, today: date) -> dict | None:
    if calc is None:
        return None
    if (
        not isinstance(calc, dict)
        or set(calc) != {"type", "slots"}
        or calc.get("type") not in CALC_TYPES
        or not isinstance(calc.get("slots"), dict)
        or not calc["slots"]
    ):
        return None

    kind = calc["type"]
    slots = dict(calc["slots"])
    allowed = {
        "annual_leave": {"hire_date", "as_of", "service_years"},
        "overtime": {
            "ordinary_wage",
            "base_pay",
            "overtime_hours",
            "night_hours",
            "holiday_hours",
        },
        "family_allowance": {"grade", "family"},
        "business_trip": {"nights", "days", "region"},
    }[kind]
    if set(slots) - allowed:
        return None

    if kind == "annual_leave":
        if "service_years" in slots and ({"hire_date", "as_of"} & set(slots)):
            return None
        if "service_years" in slots:
            value = slots["service_years"]
            if isinstance(value, dict):
                if (
                    not value
                    or set(value) - {"years", "months"}
                    or any(not _nonnegative_number(part) for part in value.values())
                    or any(not float(part).is_integer() for part in value.values())
                    or value.get("months", 0) > 11
                ):
                    return None
            elif not _nonnegative_number(value):
                return None
        else:
            for key in ("hire_date", "as_of"):
                if key in slots:
                    value = slots[key]
                    try:
                        normalized_date = date.fromisoformat(value)
                    except (TypeError, ValueError):
                        return None
                    slots[key] = normalized_date.isoformat()
            if "hire_date" in slots and "as_of" not in slots:
                slots["as_of"] = today.isoformat()
            if "hire_date" not in slots:
                return None
            if date.fromisoformat(slots["as_of"]) < date.fromisoformat(slots["hire_date"]):
                return None
    elif kind == "overtime":
        if "ordinary_wage" in slots and "base_pay" in slots:
            return None
        if any(not _nonnegative_number(value) for value in slots.values()):
            return None
    elif kind == "family_allowance":
        if "grade" in slots and (
            not _nonnegative_number(slots["grade"])
            or not float(slots["grade"]).is_integer()
        ):
            return None
        if "family" in slots:
            family = _normalize_family(slots["family"])
            if family is None:
                return None
            slots["family"] = family
    elif kind == "business_trip":
        for key in ("nights", "days"):
            if key in slots and (
                not _nonnegative_number(slots[key])
                or not float(slots[key]).is_integer()
            ):
                return None
        if "region" in slots and (
            not isinstance(slots["region"], str) or not slots["region"].strip()
        ):
            return None

    return {"type": kind, "slots": slots}


def _validate_response(raw: str, today: date) -> dict | None:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"intent", "tools", "calc"}
        or payload.get("intent") not in INTENTS
    ):
        return None

    supplied_tools = payload.get("tools")
    if not isinstance(supplied_tools, list):
        selected_tools = None
    else:
        selected_tools = []
        for name in supplied_tools:
            if name in TOOL_NAMES and name not in selected_tools:
                selected_tools.append(name)
            else:
                log.warning("LLM 분류 응답에서 허용되지 않은 조회 도구를 제외했습니다")
    result = {
        "intent": payload["intent"],
        "tools": selected_tools,
        "calc": _validate_calc(payload["calc"], today),
    }
    if result["intent"] in {"lookup", "both"} and result["tools"] is not None:
        if "profile" not in result["tools"]:
            result["tools"].insert(0, "profile")
    if result["intent"] in {"sensitive", "refuse", "out_of_scope"}:
        result["tools"] = []
        result["calc"] = None
    return result


def classify(masked_question: str, today: date) -> dict | None:
    """Classify a masked question; return None on request or response failure."""
    if not isinstance(masked_question, str) or not isinstance(today, date):
        return None
    model = config.CLASSIFY_MODEL
    context = _evaluation_context.get()
    cache_file = _cache_path(masked_question, today, model, context[0]) if context else None
    raw = None
    request_ms = 0.0
    if cache_file is not None and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            raw = cached.get("raw_output")
            request_ms = float(cached.get("request_ms", 0))
            if isinstance(raw, str):
                context[1].cache_hits += 1
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            log.warning("LLM 분류 캐시를 읽을 수 없어 새로 요청합니다")
            raw = None

    if raw is None:
        if context:
            context[1].calls += 1
        from time import perf_counter

        started = perf_counter()
        try:
            raw = _request(masked_question, today)
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            if context:
                context[1].errors.append(type(exc).__name__)
            return None
        request_ms = (perf_counter() - started) * 1000
        if context:
            context[1].request_ms.append(request_ms)
        if cache_file is not None:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "model": model,
                "prompt_hash": prompt_hash(today),
                "question_hash": hashlib.sha256(masked_question.encode("utf-8")).hexdigest(),
                "raw_output": raw,
                "request_ms": request_ms,
            }
            temporary = cache_file.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
                newline="\n",
            )
            temporary.replace(cache_file)

    result = _validate_response(raw, today)
    if result is None and context:
        try:
            json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            context[1].json_failures += 1
            context[1].raw_json_failures.append((masked_question, raw))
        else:
            context[1].validation_failures += 1
    return result
