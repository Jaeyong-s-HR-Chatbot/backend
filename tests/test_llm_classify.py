import json
from collections import Counter
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config, tools
from app.graph import llm_classify, nodes

ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "tests" / "fixtures" / "rules_calc.json"
DATA_PATH = ROOT / "eval" / "intent" / "intent_cases.jsonl"


@pytest.fixture(autouse=True)
def rule_mode(monkeypatch):
    monkeypatch.setattr(config, "CLASSIFY_MODE", "rule")
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


def test_rule_mode_does_not_call_llm(monkeypatch):
    monkeypatch.setattr(
        llm_classify,
        "classify",
        lambda *args: pytest.fail("rule mode called the LLM"),
    )

    assert nodes.classify(_state("팀장에게 폭언을 들었어요")) == {"intent": "sensitive"}


@pytest.mark.parametrize(
    "question",
    ["팀장에게 폭언을 들었어요", "제 사번은 E1584예요."],
)
def test_hybrid_short_circuits_rule_sensitive_and_no_question(question, monkeypatch):
    monkeypatch.setattr(config, "CLASSIFY_MODE", "hybrid")
    calls = []
    monkeypatch.setattr(
        llm_classify,
        "classify",
        lambda *args: calls.append(args) or {"intent": "rule", "tools": [], "calc": None},
    )

    result = nodes.classify(_state(question))

    assert calls == []
    assert result["intent"] == ("sensitive" if "폭언" in question else "rule")


@pytest.mark.parametrize("intent", ["sensitive", "refuse"])
def test_hybrid_accepts_llm_sensitive_classification(monkeypatch, intent):
    monkeypatch.setattr(config, "CLASSIFY_MODE", "hybrid")
    monkeypatch.setattr(
        llm_classify,
        "classify",
        lambda *args: {"intent": intent, "tools": [], "calc": None},
    )

    assert nodes.classify(_state("광역시 출장비 기준을 알려 줘"))["intent"] == intent


def test_hybrid_failure_falls_back_to_rule_and_warns(monkeypatch, caplog):
    monkeypatch.setattr(config, "CLASSIFY_MODE", "hybrid")
    monkeypatch.setattr(llm_classify, "classify", lambda *args: None)
    caplog.set_level("WARNING", logger="hr_chat")

    result = nodes.classify(_state("내 연차 잔여와 사용 기준을 같이 알려 줘"))

    assert result["intent"] == "both"
    assert "LLM 의도 분류에 실패해 규칙 결과로 대체합니다" in caplog.text


def test_llm_mode_does_not_run_rules_before_success(monkeypatch):
    monkeypatch.setattr(config, "CLASSIFY_MODE", "llm")
    monkeypatch.setattr(
        nodes,
        "_classify_rule",
        lambda state: pytest.fail("llm mode ran rules before a successful LLM call"),
    )
    monkeypatch.setattr(
        llm_classify,
        "classify",
        lambda *args: {"intent": "lookup", "tools": ["profile"], "calc": None},
    )

    result = nodes.classify(_state("내 입사일 알려 줘"))

    assert result["intent"] == "lookup"
    assert result["classifier_tools"] == ["profile"]


def test_llm_mode_falls_back_to_rules_on_failure(monkeypatch, caplog):
    monkeypatch.setattr(config, "CLASSIFY_MODE", "llm")
    monkeypatch.setattr(llm_classify, "classify", lambda *args: None)
    caplog.set_level("WARNING", logger="hr_chat")

    result = nodes.classify(_state("내 연차 잔여와 사용 기준을 같이 알려 줘"))

    assert result["intent"] == "both"
    assert "LLM 의도 분류에 실패해 규칙 결과로 대체합니다" in caplog.text


def test_classifier_receives_only_masked_question(monkeypatch):
    monkeypatch.setattr(config, "CLASSIFY_MODE", "hybrid")
    received = []
    monkeypatch.setattr(
        llm_classify,
        "classify",
        lambda question, today: received.append(question)
        or {"intent": "rule", "tools": [], "calc": None},
    )

    nodes.classify(_state("제 번호 010-1234-5678인데 출장비 기준을 알려 줘"))

    assert received and "[전화]" in received[0]
    assert "010-1234-5678" not in received[0]


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


def test_llm_selected_tools_still_use_token_employee_id(monkeypatch):
    captured = {}

    def fake_run_lookups(emp_id, question, selected_tools=None):
        captured.update(
            emp_id=emp_id,
            question=question,
            selected_tools=selected_tools,
        )
        return {}, ["profile", "payroll"]

    monkeypatch.setattr(tools, "run_lookups", fake_run_lookups)
    result = nodes.lookup(
        {
            "employee_id": "E1584",
            "masked_question": "급여 알려 줘",
            "classifier_tools": ["profile", "payroll"],
        }
    )

    assert captured == {
        "emp_id": "E1584",
        "question": "급여 알려 줘",
        "selected_tools": ["profile", "payroll"],
    }
    assert result["tools_used"] == ["profile", "payroll"]


def test_selected_lookup_tools_filter_keyword_matches(monkeypatch):
    calls = []

    def fake_profile(emp_id):
        calls.append(("profile", emp_id))
        return {"부서": "신호팀"}

    def fake_payroll(emp_id):
        calls.append(("payroll", emp_id))
        return {"급여월": "2026-10"}

    def fake_family(emp_id):
        calls.append(("family", emp_id))
        return {"가족": []}

    monkeypatch.setattr(tools, "get_profile", fake_profile)
    monkeypatch.setattr(
        tools,
        "TOOL_KEYWORDS",
        [
            (("급여",), "payroll", fake_payroll),
            (("가족수당",), "family", fake_family),
        ],
    )

    data, used = tools.run_lookups(
        "E1584",
        "급여와 가족수당",
        selected_tools=["profile", "payroll"],
    )

    assert calls == [("profile", "E1584"), ("payroll", "E1584")]
    assert used == ["profile", "payroll"]
    assert "가족" not in data


def test_fake_lookup_respects_selected_tools(monkeypatch):
    monkeypatch.setattr(config, "USE_DB", False)

    result = nodes.lookup(
        {
            "employee_id": "E1584",
            "masked_question": "내 연차 알려 줘",
            "classifier_tools": ["profile"],
        }
    )

    assert set(result["lookup"]) == {"기본정보"}
    assert result["tools_used"] == ["profile"]


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


def test_few_shot_examples_are_dev_only_masked_and_pair_disjoint_from_test():
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
        "event": 2,
        "sensitive": 2,
        "refuse": 2,
        "no_question": 2,
    }
    assert len(calc_examples) <= 4
    for example in examples:
        source = cases[example["id"]]
        assert source["split"] == "dev"
        assert example["id"].rsplit("-", 1)[0] not in test_pairs
        masked = nodes.guard(
            {
                "employee_id": "E1584",
                "own_name": "이서준",
                "question": source["question"],
            }
        )["masked_question"]
        assert example["question"] == masked
    assert sum(example["calc"] is not None for example in examples) == 4


@pytest.mark.parametrize(
    "question",
    ["팀장에게 폭언을 들었어요", "제 사번은 E1584예요."],
)
def test_assist_short_circuits_rule_sensitive_and_no_question(question, monkeypatch):
    monkeypatch.setattr(config, "CLASSIFY_MODE", "assist")
    calls = []
    monkeypatch.setattr(
        llm_classify,
        "classify",
        lambda *args: calls.append(args) or {"intent": "rule", "tools": [], "calc": None},
    )

    result = nodes.classify(_state(question))

    assert calls == []
    assert result["intent"] == ("sensitive" if "폭언" in question else "rule")


@pytest.mark.parametrize("intent", ["sensitive", "refuse"])
def test_assist_accepts_llm_safety_classification(monkeypatch, intent):
    monkeypatch.setattr(config, "CLASSIFY_MODE", "assist")
    monkeypatch.setattr(
        llm_classify,
        "classify",
        lambda *args: {"intent": intent, "tools": ["profile", "payroll"], "calc": None},
    )

    result = nodes.classify(_state("광역시 출장비 기준을 알려 줘"))

    assert result["intent"] == intent
    assert result["classifier_tools"] == []
    assert result["classifier_calc"] is None


def test_assist_keeps_rule_intent_and_keyword_tools_but_uses_llm_calc(monkeypatch):
    monkeypatch.setattr(config, "CLASSIFY_MODE", "assist")
    calc = {"type": "annual_leave", "slots": {"hire_date": "2023-01-01", "as_of": "2026-11-12"}}
    monkeypatch.setattr(
        llm_classify,
        "classify",
        lambda *args: {"intent": "rule", "tools": [], "calc": calc},
    )

    result = nodes.classify(_state("내 연차 잔여와 사용 기준을 같이 알려 줘"))

    assert result["intent"] == "both"           # LLM 이 rule 이라고 해도 규칙 결과를 유지
    assert "classifier_tools" not in result      # 조회 도구는 키워드 규칙이 고름
    assert result["classifier_calc"] == calc


def test_assist_failure_falls_back_to_rule_and_warns(monkeypatch, caplog):
    monkeypatch.setattr(config, "CLASSIFY_MODE", "assist")
    monkeypatch.setattr(llm_classify, "classify", lambda *args: None)
    caplog.set_level("WARNING", logger="hr_chat")

    result = nodes.classify(_state("내 연차 잔여와 사용 기준을 같이 알려 줘"))

    assert result == {"intent": "both"}
    assert "LLM 의도 분류에 실패해 규칙 결과로 대체합니다" in caplog.text
