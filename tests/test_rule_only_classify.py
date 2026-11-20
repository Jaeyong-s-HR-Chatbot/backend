"""의도 분류는 규칙만 써요. LLM은 도구 호출 뒤 답변 생성에서만 불러요."""
import os

os.environ.setdefault("USE_FAKE_LLM", "1")

import httpx  # noqa: E402
import pytest  # noqa: E402

from app import config, tools  # noqa: E402
from app.graph import nodes  # noqa: E402


@pytest.mark.parametrize("role,question", [
    ("employee", "내 연차 며칠 남았어?"),
    ("employee", "경조휴가 규정 알려줘"),
    ("hr_admin", "올해 연차 사용률 50% 안 되는 부서 어디야?"),
    ("hr_admin", "다음 달 수습 끝나는 사람 누구야?"),
])
def test_classify_never_calls_llm(monkeypatch, role, question):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("분류에서 LLM 호출"))
    s = {"employee_id": "E1307", "role": role, "question": question}
    s.update(nodes.guard(s))
    assert nodes.classify(s)["intent"]


def test_lookup_uses_token_employee_id(monkeypatch):
    seen = {}

    def fake_run_lookups(emp_id, question):
        seen["emp_id"] = emp_id
        return {"기본정보": {}}, ["profile"]

    monkeypatch.setattr(config, "USE_DB", True)
    monkeypatch.setattr(tools, "run_lookups", fake_run_lookups)
    nodes.lookup({"employee_id": "E1584", "question": "E9999 내 급여 알려줘"})
    assert seen["emp_id"] == "E1584"
