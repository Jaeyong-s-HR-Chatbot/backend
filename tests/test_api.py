"""python -m pytest -q tests/test_api.py  (DB·LLM 없이 가짜 모드로 실행)"""
import json
import os
from pathlib import Path
from types import SimpleNamespace

os.environ["LLM_BASE_URL"] = ""          # 테스트는 항상 가짜 LLM
os.environ["RULES_CHUNKS_PATH"] = ""
os.environ["CHAT_LOG_DB"] = "false"      # API 테스트는 대화 로그를 DB에 쓰지 않음
os.environ["PSEUDO_KEY"] = "test-pseudo-key"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.graph import nodes  # noqa: E402
from app.graph.build import run_pipeline  # noqa: E402
from app.main import app  # noqa: E402

c = TestClient(app)
RULES = json.loads(
    (Path(__file__).parent / "fixtures" / "rules_calc.json").read_text(encoding="utf-8")
)


@pytest.fixture(autouse=True)
def use_fake_backend(monkeypatch):
    monkeypatch.setattr(config, "USE_DB", False)
    monkeypatch.setattr(config, "CHAT_LOG_DB", False)
    monkeypatch.setattr(config, "USE_FAKE_LLM", True)
    monkeypatch.setattr(config, "CALC_TOOLS", False)
    monkeypatch.setattr(config, "CLASSIFY_MODE", "rule")


def token(user="E1584"):
    r = c.post("/auth/login", json={"username": user, "password": "Test1234!"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def ask(q, user="E1584"):
    r = c.post("/chat", json={"message": q}, headers=token(user))
    assert r.status_code == 200, r.text
    return r.json()


def test_login_fail():
    assert c.post("/auth/login", json={"username": "E1584", "password": "x"}).status_code == 401


def test_me_roles():
    assert c.get("/me", headers=token()).json()["role"] == "employee"
    assert c.get("/me", headers=token("E1003")).json()["role"] == "hr_admin"


def test_chat_needs_token():
    assert c.post("/chat", json={"message": "연차"}).status_code == 401


def test_rule_answer_has_citation():
    r = ask("광역시 1박 2일 출장비는?")
    assert r["refused"] is False and r["kind"] == "answer"
    assert r["citations"][0]["title"].startswith("여비")


def test_self_lookup():
    r = ask("내 연차 몇 개 남았어?")
    assert "11일" in r["answer"] and r["refused"] is False


def test_own_emp_id_is_not_refused():
    r = ask("E1584 연차 몇 개 남았어?")
    assert r["refused"] is False and "11일" in r["answer"]


@pytest.mark.parametrize(
    "question",
    [
        "제 전화번호 [전화]인데, 병가 때 진단서 필요해?",
        "제 전화번호 [전화], 제 사번 [본인사번]로 출장비 알려줘",
        "제 이메일 [이메일]과 계좌번호 [계좌]을 질문에 적었어요. 가족수당 규정 알려줘",
    ],
)
def test_personal_identifier_intro_does_not_trigger_lookup(question):
    state = run_pipeline("E1584", "employee", question)

    assert state["intent"] == "rule"
    assert state.get("tools_used", []) == []
    assert state["answer_masked"] is not None


@pytest.mark.parametrize(
    ("question", "expected_intent"),
    [
        ("내 연차 몇 개 남았어?", "lookup"),
        ("제 사번 [본인사번]인데, 연차 몇 개 남았어?", "lookup"),
        ("E1584 연차 몇 개 남았어?", "lookup"),
        ("[사번] 연차 몇 개 남았어?", "refuse"),
        ("제가 결혼하는데 뭐 받을 수 있어요?", "both"),   # 결혼축하금 생애 1회 한정
        ("오늘 서울 날씨 어때?", "out_of_scope"),
    ],
)
def test_personal_and_other_employee_intents_are_preserved(question, expected_intent):
    state = {"employee_id": "E1584", "question": question}
    state.update(nodes.guard(state))

    assert nodes.classify(state)["intent"] == expected_intent


@pytest.mark.parametrize(
    ("question", "expected_intent", "expected_tools"),
    [
        ("연락처는 01012345678이에요. 출장비 정산 언제 돼요?", "rule", []),
        ("연락처 010 1234 5678, 010.1234.5678 둘 다 제 번호예요", "rule", []),
        ("(010) 1234-5678이 제 번호예요", "rule", []),
        (
            "이서준(E1584), 010-1234-5678, seojun@nurirail.example, 계좌 "
            "999-12-345678입니다. 경조금 신청하려고요",
            "rule",
            [],
        ),
        ("내 연차 몇 개 남았어?", "lookup", ["profile", "leave_balance"]),
        ("제 사번 E1584인데 연차 몇 개 남았어요?", "lookup", ["profile", "leave_balance"]),
        ("제 사번은 e1584예요. 급여 알려 주세요", "lookup", ["profile", "payroll"]),
        ("이서준인데요, 복지포인트 얼마 남았어요?", "lookup", ["profile", "welfare_points"]),
        ("내 신청 결재됐어?", "lookup", ["profile", "requests"]),
        ("제 전화번호 010-1234-5678인데, 병가 때 진단서 필요해?", "rule", []),
        ("E1412 연차 몇 개 남았어?", "refuse", []),
        ("제 사번 E1584랑 동기 E1412 연차 비교해 줘", "refuse", []),
        ("박지현 과장 연봉 얼마야?", "refuse", []),
        ("팀장한테 폭언을 들었어요. 제 번호 010-1234-5678로 연락 주세요", "sensitive", []),
        ("팀장한테 폭언 들은 이서준입니다.", "sensitive", []),
        ("박지현 과장 연봉이 궁금한 이서준입니다.", "refuse", []),
        ("제 번호는 010-1234-5678입니다. 성희롱 신고하고 싶어요", "sensitive", []),
    ],
)
def test_identifier_intros_do_not_trigger_self_lookup(
    question, expected_intent, expected_tools, monkeypatch
):
    lookup_calls = []

    def fake_lookup(state):
        lookup_calls.append(state["masked_question"])
        profile = {
            "부서": "신호팀",
            "직위": "대리",
            "직급": "4급",
            "입사일": "2023-01-01",
            "근속연수": 3,
        }
        return {
            "lookup": {"기본정보": profile},
            "tools_used": expected_tools,
        }

    monkeypatch.setattr(nodes, "lookup", fake_lookup)
    state = run_pipeline("E1584", "employee", question, own_name="이서준")

    assert state["intent"] == expected_intent
    assert state.get("tools_used", []) == expected_tools
    assert bool(lookup_calls) == bool(expected_tools)


@pytest.mark.parametrize(
    "question",
    [
        "내 신청 방법 알려 줘",
        "내 신청하려면 어떤 서류가 필요해?",
        "내일 신청하려고요",
    ],
)
def test_future_application_questions_do_not_lookup(question, monkeypatch):
    lookup_calls = []
    monkeypatch.setattr(
        nodes,
        "lookup",
        lambda state: lookup_calls.append(state["masked_question"]) or {
            "lookup": {},
            "tools_used": ["profile", "requests"],
        },
    )

    state = run_pipeline("E1584", "employee", question, own_name="이서준")

    assert state["intent"] == "rule"
    assert state.get("tools_used", []) == []
    assert lookup_calls == []


@pytest.mark.parametrize(
    ("question", "expected_intent", "expected_generate"),
    [
        ("(010) 1234-5678이 제 번호예요", "rule", False),
        ("연락처 010 1234 5678, 010.1234.5678 둘 다 제 번호예요", "rule", False),
        ("주민번호 900101-1234567이고 카드는 4111-1111-1111-1111이에요", "rule", False),
        ("제 사번은 E1584예요.", "rule", False),
        ("연락처는 01012345678이에요. 출장비 정산 언제 돼요?", "rule", True),
        ("제 사번은 e1584예요. 급여 알려 주세요", "lookup", True),
        ("이서준인데요, 복지포인트 얼마 남았어요?", "lookup", True),
        ("내 연차 몇 개 남았어?", "lookup", True),
        ("병가는 1년에 며칠까지 쓸 수 있어요?", "rule", True),
        ("팀장한테 폭언 들은 이서준입니다.", "sensitive", False),
        ("박지현 과장 연봉이 궁금한 이서준입니다.", "refuse", False),
    ],
)
def test_identifier_only_questions_stop_before_generation(
    question, expected_intent, expected_generate, monkeypatch, caplog
):
    caplog.set_level("INFO", logger="hr_chat")
    generate_calls = []
    retrieve_calls = []
    original_generate = nodes.generate
    original_retrieve = nodes.retrieve

    def track_generate(state):
        generate_calls.append(state["masked_question"])
        return original_generate(state)

    def track_retrieve(state):
        retrieve_calls.append(state["masked_question"])
        return original_retrieve(state)

    monkeypatch.setattr(nodes, "generate", track_generate)
    monkeypatch.setattr(nodes, "retrieve", track_retrieve)
    state = run_pipeline("E1584", "employee", question, own_name="이서준")

    assert state["intent"] == expected_intent
    assert bool(generate_calls) is expected_generate
    if not expected_generate:
        assert question not in caplog.text
    if not expected_generate and expected_intent == "rule":
        assert state["kind"] == "refused"
        assert state["answer"] == "어떤 내용이 궁금한지 알려 주세요. 개인정보는 적지 않아도 돼요."
        assert state.get("chunks") == []
        assert state.get("tools_used") == []
        assert retrieve_calls == []


@pytest.mark.parametrize(
    ("answer", "expected_answer", "expected_kind", "expected_chunks"),
    [
        (
            "규정에서 확인되지 않아요. 인사팀에 확인해 주세요. (제 9 조 제 3 항)",
            "규정에서 확인되지 않아요. 인사팀에 확인해 주세요.",
            "refused",
            [],
        ),
        (
            "규정에서 확인되지 않아요. 인사팀에 확인해 주세요. (1)",
            "규정에서 확인되지 않아요. 인사팀에 확인해 주세요.",
            "refused",
            [],
        ),
        (
            "병가는 연 60일까지예요. (취업규칙 제23조)",
            "병가는 연 60일까지예요. (취업규칙 제23조)",
            "answer",
            ["r03-0026"],
        ),
        (
            "병가는 연 60일까지예요. 규정에서 확인되지 않아요. 인사팀에 확인해 주세요.",
            "병가는 연 60일까지예요. 규정에서 확인되지 않아요. 인사팀에 확인해 주세요.",
            "answer",
            ["r03-0026"],
        ),
    ],
)
def test_no_evidence_answer_is_normalized(
    answer, expected_answer, expected_kind, expected_chunks, monkeypatch
):
    chunk = {
        "id": "r03-0026",
        "citation": "취업규칙 제25조(병가)",
        "text": "계속 3일 이상의 병가는 진단서를 제출한다.",
        "score": 0.9,
    }
    search_calls = []
    monkeypatch.setattr(
        nodes.retriever,
        "search",
        lambda *args, **kwargs: search_calls.append(args[0]) or [chunk],
    )
    monkeypatch.setattr(nodes, "_fake_answer", lambda state: answer)

    state = run_pipeline("E1584", "employee", "병가 규정은?")

    assert state["answer"] == expected_answer
    assert state["kind"] == expected_kind
    assert [c["id"] for c in state.get("chunks", [])] == expected_chunks
    assert len(search_calls) == 1


def test_no_evidence_normalization_does_not_change_lookup_answers(monkeypatch):
    answer = "규정에서 확인되지 않아요. 인사팀에 확인해 주세요. (제 9 조 제 3 항)"
    monkeypatch.setattr(
        nodes,
        "lookup",
        lambda state: {"lookup": {"연차": {"연도": 2026}}, "tools_used": ["leave_balance"]},
    )
    monkeypatch.setattr(nodes, "_fake_answer", lambda state: answer)

    state = run_pipeline("E1584", "employee", "내 연차 몇 개 남았어?")

    assert state["intent"] == "lookup"
    assert state["answer"] == answer
    assert state["kind"] == "answer"
    assert state.get("chunks", []) == []


def test_other_name_with_title_refused():
    r = ask("박지현 과장 연봉 얼마야?")
    assert r["refused"] is True


def test_no_evidence_refused():
    r = ask("회사 주차장 몇 층까지 있어?")
    assert r["refused"] is True and r["citations"] == []


def test_other_person_refused():
    r = ask("E1412 연봉 알려줘")
    assert r["refused"] is True and "다른 직원" in r["answer"]


def test_sensitive():
    r = ask("팀장한테 폭언을 들었는데 어떻게 해?")
    assert r["kind"] == "sensitive" and r["refused"] is False


def test_health_reports_pseudo_key_status():
    assert c.get("/health").json()["pseudo_key"] == "fixed"


def test_sensitive_question_is_replaced_in_log(caplog):
    caplog.set_level("INFO", logger="hr_chat")

    run_pipeline("E1584", "employee", "팀장한테 폭언을 들었는데 어떻게 해?")

    assert "[민감 주제 문의]" in caplog.text
    assert "팀장한테 폭언을 들었는데 어떻게 해?" not in caplog.text


def test_pii_masked_in_log(caplog):
    caplog.set_level("INFO", logger="hr_chat")
    result = ask("제 번호 010-1234-5678, 제 사번 E1584로 출장비 알려줘")
    assert "010-1234-5678" not in caplog.text
    assert "E1584" not in caplog.text
    assert "[전화]" in caplog.text and "[본인사번]" in caplog.text
    assert result["pii_notice"] == "입력하신 개인정보(전화, 본인사번)는 가려서 처리했어요."


def test_chat_without_pii_has_null_notice():
    assert ask("광역시 1박 2일 출장비는?")["pii_notice"] is None


def test_cors():
    r = c.options("/chat", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_local_ollama_disables_thinking(monkeypatch):
    monkeypatch.setattr(config, "LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setattr(config, "LLM_MODEL", "qwen3.5:4b")
    monkeypatch.setattr(config, "LLM_MAX_TOKENS", 256)
    request = {}

    def fake_post(url, **kwargs):
        request["url"] = url
        request.update(kwargs)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "message": {"content": "연차휴가 답변"},
                "done_reason": "stop",
            },
        )

    monkeypatch.setattr(nodes.httpx, "post", fake_post)
    answer = nodes._llm_answer({
        "masked_question": "광역시 1박 2일 출장비는?",
        "chunks": [{"citation": "출장 규정", "text": "근거", "canned": "합치면 최대 18만원이에요."}],
    })

    assert answer == "연차휴가 답변"
    assert request["url"] == "http://localhost:11434/api/chat"
    assert request["json"]["think"] is False
    assert request["json"]["options"]["num_predict"] == 256
    assert "합치면 최대 18만원이에요." in request["json"]["messages"][1]["content"]


def test_local_ollama_warns_when_answer_is_truncated(monkeypatch, caplog):
    monkeypatch.setattr(config, "LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setattr(config, "LLM_MAX_TOKENS", 256)

    def fake_post(url, **kwargs):
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "message": {"content": "잘린 답변"},
                "done_reason": "length",
            },
        )

    monkeypatch.setattr(nodes.httpx, "post", fake_post)
    caplog.set_level("WARNING", logger="hr_chat")

    answer = nodes._llm_answer({"masked_question": "연차 규정은?", "chunks": []})

    assert answer == "잘린 답변"
    assert "LLM_MAX_TOKENS=256" in caplog.text
    assert "잘린 답변" not in caplog.text


def test_chat_openai_uses_configured_max_tokens(monkeypatch):
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setattr(config, "LLM_MAX_TOKENS", 321)
    captured = {}

    class FakePrompt:
        @classmethod
        def from_messages(cls, messages):
            return cls()

        def __or__(self, llm):
            return SimpleNamespace(
                invoke=lambda values: SimpleNamespace(content="답변")
            )

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setitem(
        __import__("sys").modules,
        "langchain_core.prompts",
        SimpleNamespace(ChatPromptTemplate=FakePrompt),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "langchain_openai",
        SimpleNamespace(ChatOpenAI=FakeChatOpenAI),
    )

    answer = nodes._llm_answer({"masked_question": "질문", "chunks": []})

    assert answer == "답변"
    assert captured["max_tokens"] == 321


def test_trip_expense_calculation():
    chunks = nodes.retriever.search("광역시 출장비", k=1)
    result = nodes.calc({
        "masked_question": "광역시 1박 2일 출장비는?",
        "intent": "rule",
        "chunks": chunks,
    })

    assert result["calc"] == {
        "daily_allowance": 50000,
        "meal_allowance": 50000,
        "daily_allowance_and_meal": 100000,
        "lodging_cap": 80000,
        "maximum_total": 180000,
    }


@pytest.mark.parametrize(
    "question",
    [
        "2023-01-01에 입사했는데 2026.11.12 기준 연차가 며칠이에요?",
        "2023.01.01에 입사했는데 2026/11/12 기준 연차가 며칠이에요?",
        "2023/01/01에 입사했는데 2026년 11월 12일 기준 연차가 며칠이에요?",
        "2023년 1월 1일에 입사했는데 2026-11-12 기준 연차가 며칠이에요?",
    ],
)
def test_calc_tools_on_calculates_annual_leave_from_question_dates(question, monkeypatch):
    rules_path = Path(__file__).parent / "fixtures" / "rules_calc.json"
    monkeypatch.setattr(config, "CALC_TOOLS", True)
    monkeypatch.setattr(config, "RULES_PATH", str(rules_path))

    result = nodes.calc({"masked_question": question})

    assert result["calc"]["종류"] == "연차 일수"
    assert result["calc"]["입력"] == {"입사일": "2023-01-01", "기준일": "2026-11-12"}
    assert result["calc"]["결과"]["부여일수"] == 16


def test_calc_tools_off_and_questions_without_explicit_hire_date_do_not_calculate(monkeypatch):
    question = "2023-01-01에 입사했는데 2026.11.12 기준 연차가 며칠이에요?"
    rules_path = Path(__file__).parent / "fixtures" / "rules_calc.json"
    monkeypatch.setattr(config, "RULES_PATH", str(rules_path))

    assert nodes.calc({"masked_question": question}) == {}
    monkeypatch.setattr(config, "CALC_TOOLS", True)
    assert nodes.calc({"masked_question": "내 연차 몇 개 남았어?"}) == {}
    assert nodes.calc({"masked_question": "병가는 1년에 며칠까지 쓸 수 있어요?"}) == {}


def test_calc_tools_requires_ordinary_wage_not_basic_pay(monkeypatch):
    rules_path = Path(__file__).parent / "fixtures" / "rules_calc.json"
    monkeypatch.setattr(config, "CALC_TOOLS", True)
    monkeypatch.setattr(config, "RULES_PATH", str(rules_path))

    basic = nodes.calc({
        "masked_question": "기본급 3,200,000원이면 시간외 10시간 수당은 얼마예요?"
    })["calc"]
    ordinary = nodes.calc({
        "masked_question": "통상임금 3,200,000원이면 시간외 10시간 수당은 얼마예요?"
    })["calc"]

    assert basic["입력 부족"] == {
        "필요한 값": "통상임금",
        "통상임금 구성": RULES["ordinary_wage"]["_status"],
        "시간급 계산식": "통상임금 ÷ 월 소정근로시간 (209시간)",
    }
    assert ordinary["결과"]["합계"] == 229_660


def test_calc_tools_accepts_million_won_and_night_holiday_hours(monkeypatch):
    rules_path = Path(__file__).parent / "fixtures" / "rules_calc.json"
    monkeypatch.setattr(config, "CALC_TOOLS", True)
    monkeypatch.setattr(config, "RULES_PATH", str(rules_path))

    result = nodes.calc({
        "masked_question": "통상임금 320만원, 연장 10시간, 야간 4시간, 휴일 3시간 수당은?"
    })["calc"]

    assert result["결과"]["연장수당"] == 229_660
    assert result["결과"]["야간가산"] == 30_620
    assert result["결과"]["휴일수당"] == 68_890
    assert result["결과"]["합계"] == 329_170


@pytest.mark.parametrize("pay_label", ["통상임금", "기본급"])
def test_calc_tools_rejects_holiday_hours_above_rule_limit(pay_label, monkeypatch):
    rules_path = Path(__file__).parent / "fixtures" / "rules_calc.json"
    monkeypatch.setattr(config, "CALC_TOOLS", True)
    monkeypatch.setattr(config, "RULES_PATH", str(rules_path))
    limit = nodes.calc_rules.holiday_hours_limit(RULES)
    assert limit is not None

    result = nodes.calc({
        "masked_question": (
            f"{pay_label} 3,200,000원이면 시간외 10시간, 휴일 {limit + 1}시간 수당은 얼마예요?"
        )
    })

    assert result["calc"] == {
        "입력 부족": f"휴일 {limit}시간 초과분의 배율이 기준표에 없음"
    }
