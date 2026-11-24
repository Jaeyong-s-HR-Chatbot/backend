"""임직원 민감 질문 규칙 감지: 단어가 달라도(따돌리는) 피해 호소 표현이면 잡고, 규정만 묻는 말은 일반 규정으로 보내요.
python -m pytest -q tests/test_sensitive_rule.py"""
import os

os.environ["LLM_BASE_URL"] = ""
os.environ["PSEUDO_KEY"] = "test-pseudo-key"

import pytest  # noqa: E402

from app.graph import nodes  # noqa: E402


def _intent(q: str) -> str:
    return nodes._classify_rule({"masked_question": q, "question": q})["intent"]


@pytest.mark.parametrize("q", [
    "동료들이 저를 따돌리는 것 같아 너무 힘들어요",
    "팀원들이 저를 일부러 피하고 업무 정보를 공유하지 않아요",
    "회의마다 제 의견만 무시하고 단체 대화방에서도 빼요",
    "상사가 큰소리로 위협해 출근하는 게 두려워요",
    "관리자가 반복해서 모욕적인 말을 해 잠을 못 자요",
    "동료가 퇴근 후에도 사적인 메시지를 계속 보내 불편해요",
    "상사가 개인적인 만남을 반복해서 요구해 부담스러워요",
    "징계 통보를 받았는데 억울해서 상담하고 싶어요",
    "성희롱을 당했는데 신고 절차와 상담 창구를 알려 주세요",       # 단어 + 피해 → 민감
    "팀장한테 폭언을 들었는데 어떻게 해?",
])
def test_victim_expressions_are_sensitive(q):
    assert _intent(q) == "sensitive"


@pytest.mark.parametrize("q", [
    "괴롭힘 신고 절차가 어떻게 돼?",
    "직장 내 성희롱 예방 규정이 뭐야?",
    "고충처리위원회 담당 부서가 어디야?",
    "징계위원회 개최 절차가 어떻게 돼?",                              # 징계는 민감 단어가 아님
])
def test_rule_only_questions_stay_rule(q):
    assert _intent(q) == "rule"


@pytest.mark.parametrize("q", [
    "내 연차 며칠 남았어?",
    "육아휴직 신청 절차를 알려 줘",
    "다른 직원들은 연차를 어떻게 쓰나요?",                            # 일반 질문에 '피하'·'빼' 단어가 섞여도 민감이 아님
    "연차를 빼고 계산하면 근속연수가 어떻게 돼?",
])
def test_ordinary_questions_are_not_sensitive(q):
    assert _intent(q) != "sensitive"
