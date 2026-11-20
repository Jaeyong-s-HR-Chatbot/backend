"""보안문서(노사회의록·징계회의록) 필요 판단"""
import os

os.environ.setdefault("USE_FAKE_LLM", "1")

import pytest  # noqa: E402

from app.graph import conf_doc  # noqa: E402
from app.graph import nodes  # noqa: E402


@pytest.mark.parametrize("q", [
    "지난 노사협의회 회의록 보여줘",
    "이번 노사협의회에서 임금 인상 어떻게 합의됐어?",
    "노조가 요구한 사항이 뭐였어?",
    "3차 단체교섭 쟁점 정리해 줘",
    "노사협의회에서 논의된 안건 알려줘",
])
def test_labor_minutes(q):
    assert conf_doc.detect(q) == {"conf_doc": "labor_minutes", "conf_doc_role": "HR_LABOR"}


@pytest.mark.parametrize("q", [
    "지난달 징계위원회 회의록 보여줘",
    "올해 징계 받은 직원 명단",
    "최근 징계위원회 심의 결과 알려줘",
    "징계 대상자 소명 내용 정리해 줘",
    "누가 징계받았어?",
])
def test_discipline_minutes(q):
    assert conf_doc.detect(q) == {"conf_doc": "discipline_minutes", "conf_doc_role": "HR_DISCIPLINE"}


@pytest.mark.parametrize("q", [
    "징계 절차가 어떻게 돼?",
    "징계 시효가 몇 년이야?",
    "징계 받은 사람 보수 얼마나 깎여?",
    "징계의결 요구서 받으면 언제까지 의결해야 돼?",
    "무단결근 며칠이면 징계 대상이야?",
    "노사협의회는 몇 달마다 열려?",
    "노사협의회 근로자위원은 어떻게 뽑아?",
    "회의실 예약은 어디서 해요?",
    "연차 며칠 남았어?",
])
def test_rule_questions_are_not_confidential(q):
    assert conf_doc.detect(q) == {}


@pytest.mark.parametrize("role", ["employee", "hr_admin"])
def test_classify_marks_conf_doc_first(role):
    s = {"employee_id": "E1307", "role": role, "question": "지난달 징계위원회 회의록 보여줘"}
    s["masked_question"] = s["question"]
    r = nodes.classify(s)
    assert r["conf_doc"] == "discipline_minutes" and r["conf_doc_role"] == "HR_DISCIPLINE" and r["intent"]
    s["question"] = s["masked_question"] = "징계 절차가 어떻게 돼?"
    assert "conf_doc" not in nodes.classify(s)
