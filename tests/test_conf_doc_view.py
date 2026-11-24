import json
from contextlib import contextmanager
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import auth, conf_api, conf_store, config, roles
from app.main import app


def doc(category="노사", level="L2", kind="회의록"):
    return {
        "doc_id": "test-doc", "title": "가상 회의록", "confidential": True,
        "doc_no": "", "owner_dept": "", "issued_at": date(2026, 10, 22),
        "status": "ready", "doc_kind": kind, "category": category, "level": level,
        "allowed_roles": [], "subject_emp_id": None, "participants": [],
    }


def segment(article, text, *, entities=None, speaker=None, sen=None, chunk_type="항목",
            level="L2", review_required=False):
    return {
        "seq": 1, "article": article, "chunk_type": chunk_type, "text": text,
        "speaker_label": speaker, "level": level, "entities": entities or [],
        "og": [], "pos": [], "qi": [], "sen": sen, "review_required": review_required,
    }


@pytest.fixture
def api(monkeypatch):
    state = {"doc": doc(), "segments": [], "read": 0, "logs": []}
    users = ("E1422", "E1545", "E1622", "E1686", "E1707", "E1007")
    role_map = {
        "E1622": {"EMPLOYEE", "SYS_ADMIN"}, "E1686": {"EMPLOYEE", "HR_DISCIPLINE"},
        "E1707": {"EMPLOYEE", "HR_LABOR"},
    }
    monkeypatch.setattr(config, "USE_DB", False)
    monkeypatch.setattr(auth, "USERS", {
        emp: {"employee_id": emp, "name": "가상", "dept": "가상", "grade": "가상", "role": "employee"}
        for emp in users
    })
    monkeypatch.setattr(roles, "get_roles", lambda emp: role_map.get(emp, {"EMPLOYEE"}))

    @contextmanager
    def read(_doc_id):
        def segments():
            state["read"] += 1
            return state["segments"]
        yield state["doc"], segments

    monkeypatch.setattr(conf_store, "read_document", read)
    monkeypatch.setattr(conf_store, "log_view", lambda *args: state["logs"].append(args))
    with TestClient(app) as client:
        yield client, state


def get(client, emp, doc_id="test-doc", token_role="employee"):
    token = auth.create_token(emp, token_role)
    return client.get(f"/documents/{doc_id}", headers={"Authorization": f"Bearer {token}"})


def test_masked_response_never_contains_hidden_text(api):
    client, state = api
    text = "가상인 참석"
    state["segments"] = [
        segment("근로자위원", text, entities=[
            {"field": "text", "type": "PS", "start": 0, "end": 3, "text": "가상인"}
        ]),
        segment("보고사항", "가상민감원문", sen="건강"),
    ]
    response = get(client, "E1422")
    assert response.status_code == 200
    data = response.json()
    assert data["view"] == "redacted" and data["redacted_count"] == 2
    raw = json.dumps(data, ensure_ascii=False)
    assert "가상인" not in raw and "가상민감원문" not in raw
    assert data["sections"][0]["blocks"][0]["segments"][0] == {"redacted": True, "level": 3}


def test_full_view_and_l4_access_log(api):
    client, state = api
    state["doc"] = doc("징계", "L4")
    state["segments"] = [segment("안건", "가상 원문")]
    response = get(client, "E1686")
    assert response.status_code == 200
    data = response.json()
    assert data["view"] == "original" and data["redacted_count"] == 0
    assert data["sections"][0]["blocks"][0]["segments"] == [{"text": "가상 원문"}]
    assert len(state["logs"]) == 1
    actor, doc_id, mode = state["logs"][0]
    assert actor.startswith("P-") and doc_id == "test-doc" and mode == "full"


def test_subject_sees_only_specified_items(api):
    client, state = api
    state["doc"] = dict(doc("징계", "L4"), subject_emp_id="E1545")
    state["segments"] = [
        segment("징계대상자 소속", "가상부서"), segment("의결사항", "가상결과"),
        segment("출석위원", "가상비밀이름"), segment("본문", "가상비밀본문"),
    ]
    response = get(client, "E1545")
    assert response.status_code == 200
    data = response.json()
    raw = json.dumps(data, ensure_ascii=False)
    assert "가상부서" in raw and "가상결과" in raw
    assert "가상비밀이름" not in raw and "가상비밀본문" not in raw
    assert data["redacted_count"] == 2 and data["view"] == "redacted"
    assert not state["logs"]


def test_transcript_sections_and_speaker_redaction(api):
    client, state = api
    state["doc"] = doc("노사", "L2", "녹취록")
    state["segments"] = [
        segment("회의일시", "2026", chunk_type="회의정보"),
        segment("발언 1", "발언 내용", speaker="위원 가상인", chunk_type="발언",
                entities=[{"field": "speaker_label", "type": "PS", "start": 3, "end": 6, "text": "가상인"}]),
    ]
    data = get(client, "E1422").json()
    assert [section["heading"] for section in data["sections"]] == ["회의 정보", "발언"]
    assert "가상인" not in json.dumps(data, ensure_ascii=False)
    assert data["redacted_count"] == 1


def test_nli_level_survives_preview_and_hides_entire_utterance(api):
    client, state = api
    state["doc"] = doc("노사", "L2", "녹취록")
    state["segments"] = [
        segment("발언 1", "가상 기밀 발언", speaker="위원 가상", chunk_type="발언",
                level="L3", review_required=True)
    ]
    response = get(client, "E1422")
    assert response.status_code == 200
    body = response.json()
    assert "가상 기밀 발언" not in json.dumps(body, ensure_ascii=False)
    assert "위원 가상" not in json.dumps(body, ensure_ascii=False)
    assert body["sections"][0]["blocks"][0]["segments"] == [{"redacted": True, "level": 3}]


def test_unreviewed_high_level_preview_retains_old_masking(api):
    client, state = api
    state["segments"] = [
        segment("보고사항", "가상인 참석", level="L4", entities=[
            {"field": "text", "type": "PS", "start": 0, "end": 3, "text": "가상인"}
        ])
    ]
    response = get(client, "E1422")
    assert response.status_code == 200
    assert response.json()["sections"][0]["blocks"][0]["segments"] == [
        {"redacted": True, "level": 3}, {"text": " 참석"},
    ]


def test_forbidden_status_not_found_and_forged_role(api):
    client, state = api
    state["doc"] = doc("징계", "L4")
    assert get(client, "E1422", token_role="hr_admin").status_code == 403
    assert get(client, "E1622").status_code == 403
    assert state["read"] == 0
    state["doc"]["status"] = "processing"
    assert get(client, "E1686").status_code == 409
    assert state["read"] == 0
    state["doc"] = None
    assert get(client, "E1686").status_code == 404
    assert get(client, "E1686", "bad!id").status_code == 404


def test_forged_frontend_role_does_not_change_masked_response(api):
    client, state = api
    state["segments"] = [segment("근로자위원", "가상인", entities=[
        {"field": "text", "type": "PS", "start": 0, "end": 3, "text": "가상인"}
    ])]
    token = auth.create_token("E1422", "hr_admin")
    response = client.get(
        "/documents/test-doc", params={"role": "HR_LABOR", "clearance": "4"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200 and response.json()["view"] == "redacted"
    assert "가상인" not in json.dumps(response.json(), ensure_ascii=False)


def test_participant_receives_full_l4_and_is_logged(api):
    client, state = api
    state["doc"] = dict(doc("징계", "L4"), participants=[{"emp_id": "E1007", "role": "COMMITTEE"}])
    state["segments"] = [segment("안건", "가상 원문")]
    response = get(client, "E1007")
    assert response.status_code == 200 and response.json()["view"] == "original"
    assert len(state["logs"]) == 1


def test_log_failure_warns_without_blocking_view(api, monkeypatch, caplog):
    client, state = api
    state["doc"] = doc("징계", "L4")
    state["segments"] = [segment("안건", "가상 원문")]

    def fail(*_args):
        raise OSError("가상 원문")

    monkeypatch.setattr(conf_store, "log_view", fail)
    assert get(client, "E1686").status_code == 200
    assert "view_log_failed" in caplog.text and "가상 원문" not in caplog.text


def test_invalid_annotations_do_not_leak(api):
    client, state = api
    state["segments"] = [segment("출석위원", "가상비밀", entities=[
        {"field": "text", "type": "PS", "start": 0, "end": 2, "text": "가상비밀"}
    ])]
    response = get(client, "E1422")
    assert response.status_code == 503
    assert "가상비밀" not in response.text


def test_missing_segments_fail_closed(api):
    client, _ = api
    assert get(client, "E1422").status_code == 503
