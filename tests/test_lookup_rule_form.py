"""내 데이터 단어가 있어도 규정을 묻는 말투면 lookup이 아니어야 해요 (검색을 건너뛰면 근거를 놓침).
Intent Search 평가(2026-11-23)에서 규칙이 lookup으로 정해 검색 누락이 난 문장들."""
import pytest

from app.graph import nodes


def _rule_intent(q: str) -> str:
    return nodes._classify_rule({"masked_question": q, "question": q, "role": "employee"})["intent"]


@pytest.mark.parametrize("q", [
    "내 가족수당 지금 한 달에 얼마 받는 게 맞아?",
    "연차 5일 남았는데 안 쓰면 돈으로 받나요? 언제요?",
    "정년퇴직 전에 남은 연차 몰아서 써도 되나요?",
    "제가 병으로 오래 치료받아야 하면 휴직되나요? 급여는요?",
    "퇴직할 때 남은 연차는 그냥 없어지는 건가요? 수당으로 정산해 주는지 궁금해요.",
    "제가 2024년 5월 10일 입사했고 지금 2026년 11월 9일인데 연차가 총 며칠 생겨야 정상인가요?",
])
def test_rule_form_is_not_lookup(q):
    assert _rule_intent(q) != "lookup"


@pytest.mark.parametrize("q", [
    "내 연차 며칠 남았어?",
    "제 복지포인트 잔액 알려줘",
])
def test_plain_lookup_stays_lookup(q):
    assert _rule_intent(q) == "lookup"
