import pytest

from confidential import conf_mask, ner_ext


def _depts(text):
    og = ner_ext.find_og(text, (), [])
    return [o[3] for o in ner_ext.find_dept_ref(text, og)]


@pytest.mark.parametrize("text, dept", [
    ("지난 4월 가상설비팀 작업자 한 명이 승강로에서 떨어졌습니다.", "가상설비팀"),
    ("감봉은 가상본부 위원님 한 분이셨고요.", "가상본부"),
    ("가상운영팀 직원 하나가 그날 혼자 들어갔어요.", "가상운영팀"),
])
def test_department_followed_by_person_reference_is_masked(text, dept):
    assert _depts(text) == [dept]


@pytest.mark.parametrize("text", [
    "가상설비팀은 결원이 계속 3명이던데요, 이번 채용에 들어가 있는 거죠?",
    "교번 담당 공백은 인사팀이 정기 인사 때 대체 인력을 지정합니다.",
    "결과는 인사팀에서 서면으로 통보합니다.",
    "가상설비팀입니다. 작업자는 모두 교육을 받았습니다.",
])
def test_department_without_person_reference_is_kept(text):
    assert _depts(text) == []


def _detect(monkeypatch, text):
    monkeypatch.setattr(conf_mask.ner_model, "predict_text", lambda *_args, **_kw: [])
    chunk = {"chunk_id": "c1", "chunk_type": "발언", "article": "발언 1", "text": text}
    return conf_mask.detect_text(text, chunk, "text", None, None, {})


def test_detect_text_adds_department_entity_and_masks_it(monkeypatch):
    text = "가상설비팀 작업자 한 명이 2미터 정도 떨어졌습니다."
    found = _detect(monkeypatch, text)
    assert ("OG", 0, 5, "가상설비팀") in found["entities"]
    assert ner_ext.mask_all(text, found["entities"], found["qi"]).startswith("[부서] 작업자")


def test_department_already_hidden_by_title_pair_is_not_duplicated(monkeypatch):
    found = _detect(monkeypatch, "대상자는 가상설비팀 과장 한 명입니다.")
    assert not [e for e in found["entities"] if e[0] == "OG"]
    assert any(hide for *_, hide in found["qi"])
