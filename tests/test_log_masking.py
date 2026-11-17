import json
import logging
import os
import subprocess
import sys

import pytest

from app import config, db
from app.graph import nodes
from app.masking import pseudonymize


@pytest.fixture
def payload(monkeypatch, caplog):
    monkeypatch.setattr(config, "USE_DB", False)
    monkeypatch.setattr(config, "CHAT_LOG_DB", False)
    monkeypatch.setattr(config, "SENSITIVE_LOG_PSEUDO", "keep")
    caplog.set_level(logging.INFO, logger="hr_chat")

    def write(**overrides):
        state = {
            "employee_id": "E1584",
            "masked_question": "질문",
            "intent": "rule",
            "pii_found": {},
            "answer": "답변",
            "answer_masked": "답변",
            **overrides,
        }
        caplog.clear()
        nodes.write_log(state)
        return json.loads(caplog.records[-1].message), state

    return write


def test_refuse_masks_other_names_only_in_log(payload):
    result, state = payload(
        intent="refuse",
        masked_question="박지현 과장 연봉과 최민수 팀장님의 급여, [본인이름] 과장",
        pii_found={"전화": 1, "타인이름": 2},
    )
    assert result["question_masked"] == "[타인이름] 과장 연봉과 [타인이름] 팀장님의 급여, [본인이름] 과장"
    assert result["pii_found"] == {"전화": 1, "타인이름": 4}
    assert state["masked_question"].startswith("박지현")
    assert state["pii_found"] == {"전화": 1, "타인이름": 2}


@pytest.mark.parametrize(
    ("intent", "question"),
    [
        ("rule", "박지현 과장 연봉 알려 줘"),
        ("refuse", "팀장 연봉 기준 알려 줘"),
        ("refuse", "팀장님 연봉 기준 알려 줘"),
        ("refuse", "[본인이름] 과장 연봉 알려 줘"),
        ("refuse", "박지현의 가족수당 알려 줘"),
    ],
)
def test_other_name_masking_is_limited(payload, intent, question):
    result, _ = payload(intent=intent, masked_question=question)
    assert result["question_masked"] == question
    assert "타인이름" not in result["pii_found"]


@pytest.mark.parametrize(("answer", "expected"), [
    ("조회 결과", nodes.LOOKUP_ANSWER_LOG_PLACEHOLDER),
    ("", None),
    (None, None),
])
def test_lookup_answer_placeholder(payload, answer, expected):
    result, _ = payload(tools_used=["profile"], answer=answer, answer_masked=None)
    assert result["answer_masked"] == expected


@pytest.mark.parametrize(("mode", "intent", "expected"), [
    ("keep", "sensitive", "pseudo"),
    ("drop", "sensitive", nodes.SENSITIVE_PSEUDO_LOG_PLACEHOLDER),
    ("drop", "rule", "pseudo"),
])
def test_sensitive_pseudonym_modes(payload, monkeypatch, mode, intent, expected):
    monkeypatch.setattr(config, "SENSITIVE_LOG_PSEUDO", mode)
    result, _ = payload(intent=intent)
    assert result["user_pseudo_id"] == (pseudonymize("E1584") if expected == "pseudo" else expected)
    if intent == "sensitive":
        assert result["question_masked"] == "[민감 주제 문의]"


def test_invalid_sensitive_pseudonym_setting_falls_back_to_keep():
    env = os.environ.copy()
    env["SENSITIVE_LOG_PSEUDO"] = "invalid"
    code = (
        "import json, logging; logging.basicConfig(level=logging.INFO, format='%(message)s'); "
        "from app import config; from app.graph import nodes; "
        "nodes.write_log({'employee_id':'E1584','masked_question':'x','intent':'sensitive'}); "
        "print(config.SENSITIVE_LOG_PSEUDO)"
    )
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert result.stdout.strip() == "keep"
    assert "SENSITIVE_LOG_PSEUDO 값이 올바르지 않아 keep 방식으로 동작합니다" in result.stderr
    payloads = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    assert len(payloads) == 1
    assert payloads[0]["user_pseudo_id"].startswith("P-")


def test_console_and_database_use_same_sanitized_payload(payload, monkeypatch):
    saved = []
    monkeypatch.setattr(config, "USE_DB", True)
    monkeypatch.setattr(config, "CHAT_LOG_DB", True)
    monkeypatch.setattr(db, "insert_chat_log", lambda **kwargs: saved.append(kwargs))
    result, _ = payload(intent="refuse", masked_question="박지현 과장 연봉 알려 줘")
    assert saved == [result]
