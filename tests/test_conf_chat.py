import json
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth, config, db, roles
from app.graph import conf_chat, conf_doc, conf_retriever, nodes
from app.graph.build import run_pipeline
from app.main import app
from confidential import embedding
from confidential.conf_build import CONF


def segment(doc_id, article, text, name=None):
    entities = []
    if name:
        start = text.index(name)
        entities.append({"field": "text", "type": "PS", "start": start, "end": start + len(name), "text": name})
    return {
        "chunk_id": f"{doc_id}-{article}", "doc_id": doc_id, "chunk_type": "항목", "article": article,
        "speaker_label": None, "text": text, "level": "L2", "entities": entities,
        "og": [], "pos": [], "qi": [], "sen": None, "review_required": False,
        "masked_text": text.replace(name, "[이름]") if name else text,
    }


@pytest.fixture
def setup(monkeypatch):
    docs = []
    segments = []
    for metadata in CONF["documents"]:
        d = {
            "doc_id": metadata["id"], "title": metadata["name"], "confidential": True,
            "category": metadata["category"], "doc_kind": metadata["doc_kind"], "level": metadata["level"],
            "allowed_roles": metadata.get("allowed_roles", []), "subject_emp_id": metadata.get("subject"),
            "participants": metadata.get("participants", []), "nli_status": "success",
        }
        docs.append(d)
        if metadata["category"] == "징계":
            segments.extend([
                segment(d["doc_id"], "의결사항", "징계 의결 감봉 1개월"),
                segment(d["doc_id"], "출석위원", "가상위원 참석", "가상위원"),
            ])
        else:
            segments.extend([
                segment(d["doc_id"], "의결사항", "노사 의결 임금 3.0% 인상"),
                segment(d["doc_id"], "근로자위원", "가상노무 참석", "가상노무"),
            ])

    granted = {
        "E1686": {"EMPLOYEE", "HR_DISCIPLINE"},
        "E1707": {"EMPLOYEE", "HR_LABOR"},
        "E1622": {"EMPLOYEE", "SYS_ADMIN"},
    }
    monkeypatch.setattr(roles, "get_roles", lambda emp: granted.get(emp, {"EMPLOYEE"}))
    monkeypatch.setattr(config, "USE_DB", True)
    monkeypatch.setattr(config, "CHAT_LOG_DB", False)
    monkeypatch.setattr(config, "USE_FAKE_LLM", True)
    monkeypatch.setattr(embedding, "encode", lambda _texts: None)
    monkeypatch.setattr(nodes, "retrieve", lambda _state: {"chunks": [
        {"id": "rule", "citation": "가상 규정", "text": "승진 제한 규정", "score": 0.9}
    ]})

    class Rows:
        def __init__(self, value):
            self.value = value

        def fetchall(self):
            return self.value

    class Connection:
        def execute(self, sql, params=()):
            if sql.startswith("SET"):
                return Rows([])
            if "FROM conf.documents WHERE" in sql:
                return Rows([d for d in docs if d["category"] == params[0]])
            if "FROM conf.chunks_masked m" in sql or "FROM conf.segments s" in sql:
                full, masked, subject, allowed = params[:4]
                eligible = [
                    s for s in segments
                    if s["doc_id"] in full
                    or (s["doc_id"] in masked and not s["review_required"])
                    or (s["doc_id"] in subject and s["article"] in allowed)
                ]
                if "FROM conf.segments s" in sql:
                    eligible = [s for s in eligible if s["chunk_id"] in params[4]]
                    return Rows(eligible)
                assert "m.doc_id = ANY(%s)" in sql and "m.article = ANY(%s)" in sql
                assert "m.review_required = false" in sql
                return Rows([{**s, "has_embedding": False, "title": next(d["title"] for d in docs if d["doc_id"] == s["doc_id"])}
                             for s in eligible])
            raise AssertionError("unexpected SQL")

    @contextmanager
    def connect():
        yield Connection()

    monkeypatch.setattr(db, "connect", connect)
    return docs, segments


@pytest.mark.parametrize(("emp", "expected"), [
    ("E1686", "original"), ("E1545", "redacted"), ("E1007", "original"),
])
def test_discipline_roles_get_authorized_results(setup, emp, expected):
    state = run_pipeline(emp, "employee", "3차 징계위원회 결과가 뭐였어?")
    assert state["kind"] == "answer"
    assert "감봉 1개월" in state["answer"]
    assert state["documents"] and all(doc["view"] == expected for doc in state["documents"])
    if emp == "E1545":
        assert all(chunk["citation"].endswith("의결사항") for chunk in state["chunks"])
        assert "가상위원" not in json.dumps(state["documents"], ensure_ascii=False)


@pytest.mark.parametrize("emp", ["E1422", "E1243", "E1622"])
def test_discipline_denies_other_people(setup, caplog, emp):
    state = run_pipeline(emp, "employee", "3차 징계위원회 결과가 뭐였어?")
    assert state["kind"] == "refused" and state["answer"] == conf_chat.NO_ACCESS
    assert state["documents"] == [] and state["chunks"] == []
    assert "감봉" not in state["answer"] and "감봉" not in caplog.text


@pytest.mark.parametrize(("emp", "expected"), [
    ("E1422", "redacted"), ("E1707", "original"), ("E1459", "original"),
])
def test_labor_roles_get_appropriate_evidence(setup, emp, expected):
    state = run_pipeline(emp, "employee", "노사협의회 4분기 회의에서 뭐가 의결됐어?")
    assert state["kind"] == "answer"
    assert "3.0%" in state["answer"]
    assert state["documents"] and all(doc["view"] == expected for doc in state["documents"])
    if emp == "E1422":
        assert "가상노무" not in state["answer"]


def test_masked_search_filters_review_flag_not_chunk_level(setup):
    _, segments = setup
    doc_id = "labor-2026-q4-minutes"
    ordinary = segment(doc_id, "보고사항", "가상키워드 일반 내용")
    ordinary["level"] = "L4"
    reviewed = segment(doc_id, "협의사항", "가상키워드 검토 내용")
    reviewed["review_required"] = True
    segments.extend([ordinary, reviewed])

    masked = conf_retriever.search("E1422", conf_doc.LABOR_MINUTES, "4분기 가상키워드")
    assert [chunk["id"] for chunk in masked["chunks"]] == [ordinary["chunk_id"]]
    assert masked["chunks"][0]["text"] == ordinary["text"]
    full = conf_retriever.search("E1707", conf_doc.LABOR_MINUTES, "4분기 가상키워드")
    assert {chunk["id"] for chunk in full["chunks"]} == {ordinary["chunk_id"], reviewed["chunk_id"]}


@pytest.mark.parametrize("nli_status", ["not_run", "failed", None])
def test_unverified_l2_transcript_is_excluded_from_masked_search(setup, nli_status):
    docs, segments = setup
    docs[:] = [doc for doc in docs if doc["doc_id"] == "labor-2026-q4-transcript"]
    docs[0]["nli_status"] = nli_status
    segments[:] = [segment(docs[0]["doc_id"], "발언", "가상키워드 내용")]
    result = conf_retriever.search("E1422", conf_doc.LABOR_MINUTES, "4분기 가상키워드")
    assert result["authorized"] is False
    assert result["chunks"] == []
    assert conf_retriever.search("E1707", conf_doc.LABOR_MINUTES, "4분기 가상키워드")["chunks"]


def test_non_conf_rule_remains_existing_route(setup, monkeypatch):
    monkeypatch.setattr(conf_retriever, "search", lambda *_args: pytest.fail("unexpected confidential search"))
    state = run_pipeline("E1422", "employee", "징계 절차가 어떻게 돼?")
    assert state["intent"] == "rule" and state.get("conf_doc") is None


def test_hidden_entity_output_and_log_are_protected(setup, monkeypatch, caplog):
    original = conf_retriever.search

    def with_hidden(*args):
        result = original(*args)
        result["hidden"].append(("가상비밀", "[이름]"))
        return result

    monkeypatch.setattr(conf_retriever, "search", with_hidden)
    monkeypatch.setattr(nodes, "generate", lambda state: {"answer": "가상비밀 말했다"})
    with caplog.at_level("INFO", logger="hr_chat"):
        state = run_pipeline("E1422", "employee", "노사협의회 4분기 회의에서 뭐가 의결됐어?")
    assert state["answer"] == "[이름] 말했다"
    assert "[기밀문서 답변]" in caplog.text and "가상비밀" not in caplog.text
    assert "[기밀문서 질문]" in caplog.text


def test_chat_log_policy_remains_first(setup, monkeypatch):
    monkeypatch.setattr(conf_retriever, "search", lambda *_args: pytest.fail("policy must stop search"))
    state = run_pipeline("E1686", "hr_admin", "징계위원회 회의록에서 누가 챗봇 대화 기록을 남겼어?")
    assert state["kind"] == "refused"
    assert state.get("refuse_reason") == "chat_log"


def test_no_external_llm_for_confidential_material(setup, monkeypatch):
    monkeypatch.setattr(config, "USE_FAKE_LLM", False)
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setattr(nodes, "generate", lambda _state: pytest.fail("external model must not be called"))
    state = run_pipeline("E1686", "employee", "3차 징계위원회 결과가 뭐였어?")
    assert state["kind"] == "refused" and state["answer"] == conf_chat.ERROR


def test_client_cannot_forge_role(setup, monkeypatch):
    monkeypatch.setattr(config, "USE_DB", False)
    monkeypatch.setattr(auth, "USERS", {
        "E1422": {"employee_id": "E1422", "name": "가상", "dept": "가상", "grade": "가상", "role": "employee"}
    })
    monkeypatch.setattr(conf_retriever.config, "USE_DB", True)
    monkeypatch.setattr(auth, "current_user", auth.current_user)
    from app.schemas import Me
    app.dependency_overrides[auth.current_user] = lambda: Me(
        employee_id="E1422", name="가상", dept="가상", grade="가상", role="employee"
    )
    try:
        with TestClient(app) as client:
            response = client.post("/chat", json={
                "message": "3차 징계위원회 결과가 뭐였어?", "role": "HR_DISCIPLINE", "clearance": 4,
            })
        assert response.status_code == 200 and response.json()["refused"] is True
        assert response.json()["documents"] == []
    finally:
        app.dependency_overrides.pop(auth.current_user, None)


def test_masked_subject_department_is_always_hidden():
    from app.conf_access import segment_view
    item = segment("test", "징계대상자 소속", "가상부서")
    assert segment_view("masked", item) == [{"redacted": True, "level": 3}]
    assert segment_view("subject", item) == [{"text": "가상부서"}]


@pytest.mark.parametrize("question", [
    "내 징계 결과가 뭐였고 승진은 언제부터 가능해?",
    "4분기 노사협의회에서 근로자위원이 뭐라고 했어?",
])
def test_missing_conf_classification_is_recognized(question):
    assert conf_doc.detect(question).get("conf_doc")


def test_labor_regular_rule_question_is_not_conf():
    state = {"employee_id": "E1422", "role": "employee", "question": "노사협의회는 몇 달마다 열려?"}
    state.update(nodes.guard(state))
    assert nodes.classify(state) == {"intent": "rule"}


def test_mixed_question_sends_both_evidence_types(setup, monkeypatch):
    seen = {}

    def generate(state):
        seen.update(state)
        return {"answer": "가상 결과"}

    monkeypatch.setattr(nodes, "generate", generate)
    state = run_pipeline("E1545", "employee", "내 징계 결과가 뭐였고 승진은 언제부터 가능해?")
    assert state["kind"] == "answer"
    assert any(chunk.get("doc_id") for chunk in seen["chunks"])
    assert any(chunk["id"] == "rule" for chunk in seen["chunks"][:5])
    assert {doc["doc_id"] for doc in state["documents"]} == {
        chunk["doc_id"] for chunk in seen["chunks"] if chunk.get("doc_id")
    }


def test_no_matching_period_is_not_access_denied(setup):
    state = run_pipeline("E1686", "employee", "99차 징계위원회 결과가 뭐였어?")
    assert state["answer"] == conf_chat.NO_EVIDENCE


def test_hidden_material_removed_from_all_prompt_chunks(setup, monkeypatch):
    def search(*_args):
        return {
            "authorized": True, "hidden": [("가상비밀", "[이름]")],
            "documents": [{"doc_id": "doc", "title": "가상비밀", "confidential": True, "view": "redacted"}],
            "chunks": [{"id": "chunk", "doc_id": "doc", "citation": "가상비밀",
                        "text": "가상비밀", "score": 0.9}],
        }

    monkeypatch.setattr(conf_retriever, "search", search)
    def generate(state):
        assert "가상비밀" not in json.dumps(state["chunks"], ensure_ascii=False)
        assert "가상비밀" not in state["llm_question"]
        return {"answer": "가상비밀"}
    monkeypatch.setattr(nodes, "generate", generate)
    state = run_pipeline("E1422", "employee", "가상비밀 노사협의회 4분기 회의에서 뭐가 의결됐어?")
    assert state["answer"] == "[이름]"
    assert "가상비밀" not in json.dumps(state["documents"], ensure_ascii=False)


def test_hidden_material_removed_from_mixed_lookup_and_rule(setup, monkeypatch):
    monkeypatch.setattr(conf_retriever, "search", lambda *_args: {
        "authorized": True, "hidden": [("가상비밀", "[이름]")],
        "documents": [{"doc_id": "doc", "title": "문서", "confidential": True, "view": "redacted"}],
        "chunks": [{"id": "chunk", "doc_id": "doc", "citation": "문서", "text": "근거", "score": 0.9}],
    })
    monkeypatch.setattr(nodes, "lookup", lambda _state: {"lookup": {"항목": "가상비밀"}, "tools_used": ["own"]})
    monkeypatch.setattr(nodes, "retrieve", lambda _state: {"chunks": [
        {"id": "rule", "citation": "규정", "text": "규정", "canned": "가상비밀", "score": 0.9}
    ]})

    def generate(state):
        assert "가상비밀" not in json.dumps([state["lookup"], state["chunks"]], ensure_ascii=False)
        assert state["intent"] == "both"
        return {"answer": "안내"}

    monkeypatch.setattr(nodes, "generate", generate)
    state = run_pipeline("E1545", "employee", "내 징계 결과와 연차, 승진 규정 알려줘")
    assert state["kind"] == "answer"


def test_embedding_checks_dimensions_and_normalization(monkeypatch):
    class Model:
        def encode(self, texts, normalize_embeddings):
            assert normalize_embeddings is True
            return [[1.0] + [0.0] * (embedding.DIMENSIONS - 1) for _ in texts]

    monkeypatch.setattr(embedding, "model", lambda: Model())
    assert len(embedding.encode(["가상"])[0]) == 1024
    monkeypatch.setattr(embedding, "model", lambda: type("Bad", (), {
        "encode": lambda self, texts, normalize_embeddings: [[1.0]]
    })())
    with pytest.raises(ValueError):
        embedding.encode(["가상"])
    monkeypatch.setattr(embedding, "model", lambda: None)
    assert embedding.encode(["가상"]) is None


def test_embedding_uses_default_model_cache(monkeypatch):
    sentinel = object()
    calls = []

    def load(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=load))
    embedding.model.cache_clear()
    try:
        assert embedding.model() is sentinel
        assert embedding.model() is sentinel
        assert calls == [((config.EMBED_MODEL,), {})]
    finally:
        embedding.model.cache_clear()


def test_missing_embedding_model_warns_and_falls_back(monkeypatch, caplog):
    def load(*_args, **_kwargs):
        raise OSError("model unavailable")

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=load))
    embedding.model.cache_clear()
    try:
        with caplog.at_level("WARNING", logger="confidential.embedding"):
            assert embedding.encode(["가상"]) is None
        assert "키워드 검색" in caplog.text
    finally:
        embedding.model.cache_clear()
