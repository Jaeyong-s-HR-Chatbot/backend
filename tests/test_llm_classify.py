"""② 임직원 분류 보조 LLM (규칙 분류 뒤 놓친 민감·타인 조회 보완 + 계산 값)"""
import json
from collections import Counter
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config
from app.graph import llm_classify, nodes

ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "tests" / "fixtures" / "rules_calc.json"
DATA_PATH = ROOT / "eval" / "intent" / "intent_cases.jsonl"


@pytest.fixture(autouse=True)
def base(monkeypatch):
    monkeypatch.setattr(config, "CALC_TOOLS", False)
    monkeypatch.setattr(config, "USE_DB", True)


def _state(question):
    state = {"employee_id": "E1584", "own_name": "이서준", "question": question}
    state.update(nodes.guard(state))
    return state


def _response(intent="rule", tools_list=None, calc=None):
    return json.dumps(
        {
            "intent": intent,
            "tools": [] if tools_list is None else tools_list,
            "calc": calc,
        },
        ensure_ascii=False,
    )


def test_invalid_tool_names_are_dropped():
    result = llm_classify._validate_response(
        _response("lookup", ["payroll", "not_a_tool"]), date(2026, 11, 15)
    )

    assert result["tools"] == ["profile", "payroll"]


@pytest.mark.parametrize(
    "calc",
    [
        {"type": "annual_leave", "slots": {"hire_date": "2026-02-30", "as_of": "2026-11-15"}},
        {"type": "overtime", "slots": {"ordinary_wage": 3200000, "overtime_hours": -1}},
    ],
)
def test_invalid_dates_and_negative_values_discard_only_calc(calc):
    result = llm_classify._validate_response(
        _response("rule", [], calc), date(2026, 11, 15)
    )

    assert result is not None
    assert result["intent"] == "rule"
    assert result["calc"] is None


def test_basic_pay_is_not_used_as_ordinary_wage(monkeypatch):
    monkeypatch.setattr(config, "CALC_TOOLS", True)
    monkeypatch.setattr(config, "RULES_PATH", str(RULES_PATH))
    state = {
        "masked_question": "기본급 3,200,000원이면 시간외 10시간 수당은 얼마예요?",
        "classifier_calc": {
            "type": "overtime",
            "slots": {"base_pay": 3200000, "overtime_hours": 10},
        },
    }

    result = nodes.calc(state)["calc"]

    assert result["입력 부족"]["필요한 값"] == "통상임금"


def test_monthly_pay_misclassified_as_ordinary_wage_still_needs_input(monkeypatch):
    monkeypatch.setattr(config, "CALC_TOOLS", True)
    monkeypatch.setattr(config, "RULES_PATH", str(RULES_PATH))
    state = {
        "masked_question": "월급 320만원이면 시간외 10시간 수당은 얼마예요?",
        "classifier_calc": {
            "type": "overtime",
            "slots": {"ordinary_wage": 3200000, "overtime_hours": 10},
        },
    }

    result = nodes.calc(state)["calc"]

    assert result["입력 부족"]["필요한 값"] == "통상임금"


def test_service_years_requires_explicit_hire_date(monkeypatch):
    monkeypatch.setattr(config, "CALC_TOOLS", True)
    state = {
        "masked_question": "입사 3년차인데 연차를 계산해 줘",
        "classifier_calc": {
            "type": "annual_leave",
            "slots": {"service_years": 3},
        },
    }

    result = nodes.calc(state)["calc"]

    assert result["입력 부족"] == {
        "필요한 값": "입사일(연·월·일)",
        "안내": "연차는 회계연도 기준이라 입사 연도와 날짜가 있어야 계산할 수 있어요.",
    }


def test_ollama_request_uses_json_schema_and_limits(monkeypatch):
    captured = {}
    raw = _response()

    def fake_post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"message": {"content": raw}},
        )

    monkeypatch.setattr(llm_classify.httpx, "post", fake_post)
    monkeypatch.setattr(config, "LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setattr(config, "CLASSIFY_MODEL", "qwen3.5:4b")
    monkeypatch.setattr(config, "CLASSIFY_TIMEOUT", 7)

    result = llm_classify.classify("마스킹된 질문", date(2026, 11, 15))

    assert result["intent"] == "rule"
    assert captured["url"] == "http://localhost:11434/api/chat"
    assert captured["json"]["format"] == llm_classify.OUTPUT_SCHEMA
    assert captured["json"]["think"] is False
    assert captured["json"]["options"] == {
        "temperature": 0,
        "num_predict": llm_classify.OUTPUT_LIMIT,
    }
    assert captured["timeout"] == 7


def test_response_cache_uses_model_prompt_and_masked_question(tmp_path, monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"message": {"content": _response()}},
        )

    monkeypatch.setattr(llm_classify.httpx, "post", fake_post)
    monkeypatch.setattr(config, "LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setattr(config, "CLASSIFY_MODEL", "qwen3.5:4b")
    with llm_classify.evaluation_cache(tmp_path) as stats:
        first = llm_classify.classify("[전화] 문의", date(2026, 11, 15))
        second = llm_classify.classify("[전화] 문의", date(2026, 11, 15))

    assert first == second
    assert calls == ["http://localhost:11434/api/chat"]
    assert stats.calls == 1
    assert stats.cache_hits == 1
    cache = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert cache["model"] == "qwen3.5:4b"
    assert cache["prompt_hash"] == llm_classify.prompt_hash(date(2026, 11, 15))
    assert cache["question_hash"]
    assert "[전화] 문의" not in json.dumps(cache, ensure_ascii=False)


def test_json_parse_failure_keeps_actual_output_in_evaluation_stats(tmp_path, monkeypatch):
    raw = "not json"

    def fake_post(url, **kwargs):
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"message": {"content": raw}},
        )

    monkeypatch.setattr(llm_classify.httpx, "post", fake_post)
    monkeypatch.setattr(config, "LLM_BASE_URL", "http://localhost:11434/v1")
    with llm_classify.evaluation_cache(tmp_path) as stats:
        result = llm_classify.classify("[전화] 문의", date(2026, 11, 15))

    assert result is None
    assert stats.json_failures == 1
    assert stats.raw_json_failures == [("[전화] 문의", raw)]


def test_few_shot_examples_are_dev_only_original_and_pair_disjoint_from_test():
    cases = {
        case["id"]: case
        for case in (
            json.loads(line)
            for line in DATA_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    examples = llm_classify._examples()
    test_pairs = {
        case_id.rsplit("-", 1)[0]
        for case_id, case in cases.items()
        if case["split"] == "test"
    }
    label_examples = [example for example in examples if example["purpose"] == "label"]
    calc_examples = [example for example in examples if example["purpose"] == "calculation"]

    assert Counter(example["intent"] for example in label_examples) == {
        "rule": 2,
        "lookup": 2,
        "both": 2,
        "out_of_scope": 2,
        "sensitive": 2,
        "refuse": 2,
        "no_question": 2,
    }
    assert len(calc_examples) <= 4
    for example in examples:
        source = cases[example["id"]]
        assert source["split"] == "dev"
        assert example["id"].rsplit("-", 1)[0] not in test_pairs
        assert example["question"] == source["question"]   # LLM은 마스킹 안 한 원문을 받으니 예시도 원문
    assert sum(example["calc"] is not None for example in examples) == 4


@pytest.fixture
def llm_on(monkeypatch):
    monkeypatch.setattr(config, "CLASSIFY_LLM", True)
    monkeypatch.setattr(config, "USE_FAKE_LLM", False)


def test_llm_off_does_not_call_llm(monkeypatch):
    monkeypatch.setattr(llm_classify, "classify", lambda *a: pytest.fail("LLM called"))
    assert nodes.classify(_state("내 연차 잔여와 사용 기준을 같이 알려 줘"))["intent"] == "both"


def test_fake_llm_does_not_call_classifier(monkeypatch):
    monkeypatch.setattr(config, "CLASSIFY_LLM", True)
    monkeypatch.setattr(config, "USE_FAKE_LLM", True)
    monkeypatch.setattr(llm_classify, "classify", lambda *a: pytest.fail("LLM called"))
    nodes.classify(_state("내 연차 잔여와 사용 기준을 같이 알려 줘"))


@pytest.mark.parametrize("question", ["팀장에게 폭언을 들었어요", "제 사번은 E1584예요."])
def test_rule_sensitive_and_no_question_skip_llm(question, monkeypatch, llm_on):
    calls = []
    monkeypatch.setattr(llm_classify, "classify",
                        lambda *args: calls.append(args) or {"intent": "rule", "tools": [], "calc": None})
    result = nodes.classify(_state(question))
    assert calls == []
    assert result["intent"] == ("sensitive" if "폭언" in question else "rule")


@pytest.mark.parametrize("intent", ["sensitive", "refuse"])
def test_llm_adds_missed_sensitive_or_refuse(monkeypatch, llm_on, intent):
    monkeypatch.setattr(llm_classify, "classify",
                        lambda *args: {"intent": intent, "tools": ["profile", "payroll"], "calc": None})
    result = nodes.classify(_state("광역시 출장비 기준을 알려 줘"))
    assert result["intent"] == intent and "classifier_calc" not in result


def test_llm_keeps_rule_intent_but_uses_llm_calc(monkeypatch, llm_on):
    calc = {"type": "annual_leave", "slots": {"hire_date": "2023-01-01", "as_of": "2026-11-12"}}
    monkeypatch.setattr(llm_classify, "classify", lambda *args: {"intent": "rule", "tools": [], "calc": calc})
    result = nodes.classify(_state("내 연차 잔여와 사용 기준을 같이 알려 줘"))
    assert result["intent"] == "both"           # LLM이 rule이라고 해도 규칙 결과를 유지
    assert result["classifier_calc"] == calc


def test_llm_failure_falls_back_to_rule_and_warns(monkeypatch, caplog, llm_on):
    monkeypatch.setattr(llm_classify, "classify", lambda *args: None)
    caplog.set_level("WARNING", logger="hr_chat")
    result = nodes.classify(_state("내 연차 잔여와 사용 기준을 같이 알려 줘"))
    assert result == {"intent": "both"}
    assert "분류 보조 LLM 호출에 실패해 규칙 결과만 씁니다" in caplog.text


def test_classifier_receives_original_question(monkeypatch, llm_on):
    seen = []
    monkeypatch.setattr(llm_classify, "classify",
                        lambda q, today: seen.append(q) or {"intent": "rule", "tools": [], "calc": None})
    nodes.classify(_state("제 전화번호 010-1234-5678인데 경조휴가 규정 알려줘"))
    assert seen and "010-1234-5678" in seen[0]


@pytest.mark.parametrize("question, rule_intent, llm_intent, expected", [
    ("광역시 출장비 기준을 알려 줘", "rule", "lookup", "lookup"),          # 규칙 rule → LLM 의도
    ("오늘 날씨 어때?", "out_of_scope", "no_question", "no_question"),   # 규칙 out_of_scope → LLM 의도
    ("내 연차 잔여와 사용 기준을 같이 알려 줘", "both", "rule", "both"),  # 규칙 both → 규칙 유지
])
def test_llm_decides_when_rule_is_rule_or_out_of_scope(monkeypatch, llm_on, question, rule_intent, llm_intent, expected):
    assert nodes._classify_rule(_state(question))["intent"] == rule_intent
    monkeypatch.setattr(llm_classify, "classify", lambda *a: {"intent": llm_intent, "tools": [], "calc": None})
    assert nodes.classify(_state(question))["intent"] == expected


def test_no_question_is_valid_llm_intent():
    assert "no_question" in llm_classify.OUTPUT_SCHEMA["properties"]["intent"]["enum"]
    r = llm_classify._validate_response(json.dumps({"intent": "no_question", "tools": ["profile"], "calc": None}),
                                        date(2026, 11, 15))
    assert r == {"intent": "no_question", "tools": [], "calc": None}


def test_pipeline_routes_llm_no_question(monkeypatch):
    from app.graph import build
    monkeypatch.setattr(nodes, "classify", lambda s: {"intent": "no_question"})
    r = build.run_pipeline("E1584", "employee", "오늘도 화이팅입니다")
    assert r["kind"] == "refused" and r["answer"] == nodes.refuse_no_question({})["answer"]
