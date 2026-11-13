"""python -m pytest -q tests/test_rag_mode.py  (SEARCH_MODE=rag 연결 확인. DB·모델 없이 동작)"""
import json
import os
from pathlib import Path

import pytest

os.environ["LLM_BASE_URL"] = ""
os.environ["HRDB_URL"] = ""
os.environ["RULES_CHUNKS_PATH"] = ""
os.environ["CHAT_LOG_DB"] = "false"

from app import config  # noqa: E402
from app.graph import retriever  # noqa: E402

RAG_ROOT = Path(config.RAG_PATH) if config.RAG_PATH else Path(__file__).resolve().parents[2] / "rag-main"
pytestmark = pytest.mark.skipif(not (RAG_ROOT / "rag" / "__init__.py").exists(), reason="rag-main 폴더 없음")

CHUNKS = [
    {"chunk_id": "r03-0026", "document_id": "r03", "doc_kind": "내부규정", "doc_name": "취업규칙", "article": "제25조",
     "article_title": "병가", "categories": ["근태관리"], "searchable": True, "effective_date": "2026-07-28",
     "text": "제25조(병가) 계속 3일 이상의 병가는 진단서를 제출한다."},
    {"chunk_id": "r05-0027", "document_id": "r05", "doc_kind": "내부규정", "doc_name": "여비규정", "article": "별표 2",
     "article_title": "", "categories": ["출장"], "searchable": True, "effective_date": "2024-01-01",
     "text": "국내 출장 숙박비 상한: 서울 10만원, 광역시 8만원."},
]


@pytest.fixture
def rag_mode(tmp_path, monkeypatch):
    f = tmp_path / "chunks.jsonl"
    f.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in CHUNKS), encoding="utf-8")
    monkeypatch.setenv("RAG_CHUNKS", str(f))
    monkeypatch.setenv("RAG_EMBED", "off")
    monkeypatch.setattr(config, "SEARCH_MODE", "rag")
    retriever._rag.cache_clear()
    yield
    retriever._rag.cache_clear()
    import rag.backend
    rag.backend._pipe = None


def test_rag_mode_returns_backend_format(rag_mode):
    assert retriever.mode() == "rag"
    hits = retriever.search("병가 쓸 때 진단서 필요해?", k=5)
    assert hits and hits[0]["id"] == "r03-0026"
    assert {"id", "citation", "revised", "text", "score"} <= set(hits[0])
    assert hits[0]["citation"].startswith("취업규칙 제25조") and 0.0 <= hits[0]["score"] <= 1.0


def test_rag_mode_falls_back_when_package_cannot_load(monkeypatch):
    monkeypatch.setenv("RAG_CHUNKS", "")              # 조각을 못 찾으면 rag 패키지가 오류를 내요
    monkeypatch.setenv("RAG_EMBED", "off")
    monkeypatch.setattr(config, "SEARCH_MODE", "rag")
    retriever._rag.cache_clear()
    import rag.backend
    rag.backend._pipe = None
    try:
        assert retriever.mode() != "rag"
        hits = retriever.search("광역시 1박 2일 출장비는?", k=5)      # 기본 검색(내장 예시)으로 동작
        assert hits and hits[0]["id"].startswith("sample-")
    finally:
        retriever._rag.cache_clear()
