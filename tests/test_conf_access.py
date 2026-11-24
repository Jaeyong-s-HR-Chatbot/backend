import pytest

from app.conf_access import access, segment_view
from confidential.conf_build import CONF
from confidential.upload_meta import CATEGORY_ROLES


IDS = [
    "discipline-2026-03-transcript", "discipline-2026-03-minutes",
    "discipline-2026-02-minutes", "labor-2026-q4-transcript",
    "labor-2026-q4-minutes", "labor-2026-q3-minutes",
]
ROLES = {
    "E1686": {"EMPLOYEE", "HR_DISCIPLINE"},
    "E1707": {"EMPLOYEE", "HR_LABOR"},
    "E1622": {"EMPLOYEE", "SYS_ADMIN"},
    "E1243": {"EMPLOYEE", "TEAM_LEAD"},
}
EXPECTED = {
    "E1422": ["deny", "deny", "deny", "deny", "masked", "masked"],
    "E1243": ["deny", "deny", "deny", "deny", "masked", "masked"],
    "E1686": ["full", "full", "full", "deny", "masked", "masked"],
    "E1707": ["deny", "deny", "deny", "full", "full", "full"],
    "E1622": ["deny", "deny", "deny", "deny", "deny", "deny"],
    "E1545": ["deny", "subject", "deny", "deny", "masked", "masked"],
    "E1007": ["full", "full", "deny", "deny", "masked", "masked"],
    "E1459": ["deny", "deny", "deny", "full", "full", "full"],
    "E1003": ["deny", "deny", "full", "full", "full", "full"],
}


@pytest.fixture
def documents():
    return {
        d["id"]: {
            "doc_kind": d["doc_kind"], "category": d["category"], "level": d["level"],
            "allowed_roles": d.get("allowed_roles", []),
            "subject_emp_id": d.get("subject"), "participants": d.get("participants", []),
        }
        for d in CONF["documents"]
    }


@pytest.mark.parametrize("emp", list(EXPECTED))
def test_demo_access_matrix(documents, emp):
    roles = ROLES.get(emp, {"EMPLOYEE"})
    assert [access(emp, roles, documents[doc_id]) for doc_id in IDS] == EXPECTED[emp]


def test_category_owner_sees_l2_even_without_allowed_roles(documents):
    doc = documents["labor-2026-q4-minutes"]
    assert doc["allowed_roles"] == []
    assert access("E1707", {"EMPLOYEE", "HR_LABOR"}, doc) == "full"
    assert access("E1686", {"EMPLOYEE", "HR_DISCIPLINE"}, doc) == "masked"


def test_category_role_mapping_is_shared(documents, monkeypatch):
    doc = documents["labor-2026-q4-minutes"]
    monkeypatch.setitem(CATEGORY_ROLES, "노사", "HR_OTHER")
    assert access("E1707", {"HR_LABOR"}, doc) == "masked"
    assert access("E1707", {"HR_OTHER"}, doc) == "full"


def test_unrecognized_metadata_denies_access(documents):
    doc = dict(documents[IDS[0]], level="unknown")
    assert access("E1686", ROLES["E1686"], doc) == "deny"
    doc = dict(documents[IDS[0]], participants=None)
    assert access("E1686", ROLES["E1686"], doc) == "deny"
    doc = dict(documents[IDS[0]], participants=[{"emp_id": "E1422", "role": "UNKNOWN"}])
    assert access("E1422", {"EMPLOYEE"}, doc) == "deny"
    doc = dict(documents[IDS[0]], doc_kind="unknown")
    assert access("E1686", ROLES["E1686"], doc) == "deny"
    doc = dict(documents[IDS[0]], allowed_roles=["SYS_ADMIN"])
    assert access("E1622", ROLES["E1622"], doc) == "deny"
    doc["allowed_roles"].append("EMPLOYEE")
    assert access("E1622", ROLES["E1622"], doc) == "deny"
    doc = dict(doc, participants=[{"emp_id": "E1622", "role": "COMMITTEE"}])
    assert access("E1622", ROLES["E1622"], doc) == "full"


def sample():
    text = "가상부서 가상직급 가상인"
    speaker = "위원 가상인"
    return {
        "article": "발언 1", "text": text, "speaker_label": speaker, "level": "L2",
        "entities": [
            {"field": "text", "type": "PS", "start": 10, "end": 13, "text": "가상인"},
            {"field": "speaker_label", "type": "PS", "start": 3, "end": 6, "text": "가상인"},
        ],
        "og": [["OG", 0, 4, "가상부서"]], "pos": [["POS", 5, 9, "가상직급"]],
        "qi": [["가상부서", "가상직급", 1, True]], "sen": None, "review_required": False,
    }


def test_masked_spans_include_speaker_and_indirect_identifier():
    result = segment_view("masked", sample())
    combined = str(result)
    assert all(value not in combined for value in ("가상인", "가상부서", "가상직급"))
    assert sum(part.get("redacted") is True for part in result) == 4
    assert all(part["level"] == 3 for part in result if "redacted" in part)


def test_sensitive_chunk_is_entirely_redacted():
    segment = sample()
    segment["sen"] = "건강"
    assert segment_view("masked", segment) == [{"redacted": True, "level": 3}]


def test_nli_elevated_segment_is_hidden_from_masked_access():
    segment = sample()
    segment["review_required"] = True
    segment["level"] = "L3"
    assert segment_view("masked", segment) == [{"redacted": True, "level": 3}]
    segment["level"] = "L4"
    assert segment_view("masked", segment) == [{"redacted": True, "level": 4}]


def test_masked_unreviewed_high_level_uses_existing_partial_masking():
    segment = sample()
    segment["level"] = "L4"
    result = segment_view("masked", segment)
    assert {"text": "위원 "} in result
    assert len(result) > 1
    assert "가상인" not in str(result)


def test_invalid_review_status_fails_closed():
    segment = sample()
    segment["review_required"] = None
    with pytest.raises(ValueError):
        segment_view("masked", segment)


def test_subject_full_and_other_segments():
    segment = sample()
    assert segment_view("subject", segment) == [{"redacted": True, "level": 4}]
    segment["article"] = "징계대상자 소속"
    assert segment_view("subject", segment)[-1] == {"text": segment["text"]}
    assert segment_view("full", segment)[-1] == {"text": segment["text"]}


def test_ambiguous_or_invalid_annotations_fail_closed():
    segment = sample()
    segment["entities"][0]["end"] = 12
    with pytest.raises(ValueError):
        segment_view("masked", segment)
    segment = sample()
    segment["og"] = []
    with pytest.raises(ValueError):
        segment_view("masked", segment)
    with pytest.raises(ValueError):
        segment_view("deny", sample())


def test_overlapping_spans_merge_without_length_hint():
    segment = sample()
    segment["speaker_label"] = None
    segment["entities"] = [
        {"field": "text", "type": "ID", "start": 0, "end": 4, "text": "가상부서"},
    ]
    result = segment_view("masked", segment)
    assert result[0] == {"redacted": True, "level": 3}
    assert "가상부서" not in str(result)
