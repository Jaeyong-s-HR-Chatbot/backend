import json
import sqlite3
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from confidential import check_ingest, conf_build, conf_mask, ingest
from scripts import backfill_conf_upload


class SQLiteConnection:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("ATTACH DATABASE ':memory:' AS conf")
        self.db.executescript(
            """
            CREATE TABLE conf.documents (
                doc_id TEXT PRIMARY KEY, title TEXT, doc_kind TEXT, form TEXT, category TEXT,
                level TEXT, allowed_roles TEXT, subject_emp_id TEXT, participants TEXT,
                pair_doc_id TEXT, issued_at TEXT, owner_dept TEXT, doc_no TEXT, confidential,
                source_file TEXT, status TEXT, chunk_count INTEGER,
                file_name TEXT, file_size INTEGER, uploaded_at TEXT, uploaded_by TEXT
            );
            CREATE TABLE conf.segments (
                chunk_id TEXT PRIMARY KEY, doc_id TEXT, seq INTEGER, chunk_type TEXT, article TEXT,
                page INTEGER, speaker_code TEXT, speaker_label TEXT, text TEXT, level TEXT,
                entities TEXT, og TEXT, pos TEXT, qi TEXT, sen TEXT
            );
            CREATE TABLE conf.chunks_masked (
                chunk_id TEXT PRIMARY KEY, doc_id TEXT, seq INTEGER, chunk_type TEXT, article TEXT,
                speaker_masked TEXT, masked_text TEXT, level TEXT, sen TEXT, allowed_roles TEXT,
                embedding TEXT, tsv TEXT
            );
            """
        )
        self.db.create_function("to_tsvector", 2, lambda _language, text: text)

    def transaction(self):
        return self.db

    def execute(self, sql, params=()):
        def adapt(value):
            if isinstance(value, Jsonb):
                return json.dumps(value.obj, ensure_ascii=False)
            if isinstance(value, list):
                return json.dumps(value, ensure_ascii=False)
            return value

        return self.db.execute(sql.replace("%s", "?").replace("::vector", ""), tuple(adapt(p) for p in params))


@pytest.fixture
def doc():
    return {
        "id": "test-document", "name": "가상 회의록", "file": "test.pdf",
        "doc_kind": "회의록", "form": "discipline_minutes", "category": "징계",
        "level": "L4", "allowed_roles": ["HR_DISCIPLINE"],
    }


def test_speaker_codes_follow_first_appearance(monkeypatch):
    text = (
        "위원장 홍길동 시작합니다.\n"
        "위원 가상인 의견입니다.\n"
        "위원장 홍길동 마치겠습니다."
    )
    monkeypatch.setattr(conf_build, "pdf_tables_and_rest", lambda _path: [(1, [], text)])
    chunks = conf_build.split_transcript({"file": "test.pdf", "name": "가상 녹취록"}, Path("."))
    assert [c["speaker"] for c in chunks] == ["S1", "S2", "S1"]
    assert [c["speaker_role"] for c in chunks] == ["위원장", "위원", "위원장"]


def test_ingest_masks_sensitive_chunks_and_replaces_previous_rows(tmp_path, monkeypatch, doc):
    monkeypatch.setattr(ingest.embedding, "encode", lambda _texts: None)
    pdf = tmp_path / "test.pdf"
    pdf.touch()
    chunks = [
        {"chunk_id": "test-document-0001", "chunk_type": "발언", "article": "발언 1",
         "page": 1, "text": "홍길동 위원이 말했습니다.", "speaker": "S1", "speaker_label": "위원 홍길동"},
        {"chunk_id": "test-document-0002", "chunk_type": "발언", "article": "발언 2",
         "page": 1, "text": "몸이 안 좋습니다.", "speaker": None, "speaker_label": None},
    ]
    monkeypatch.setattr(conf_build, "build_doc", lambda *_args: chunks)

    def fake_detect(text, chunk, field, tok, model, counts):
        name = "홍길동"
        start = text.find(name)
        entities = [("PS", start, start + len(name), name)] if start >= 0 else []
        return {"entities": entities, "og": [], "pos": [], "qi": [],
                "sen": "건강" if "몸이 안 좋" in text else None}

    monkeypatch.setattr(conf_mask, "detect_text", fake_detect)
    conn = SQLiteConnection()
    try:
        for _ in range(2):
            stats = ingest.ingest_document(pdf, doc, model=(None, None), counts={}, conn=conn)
            assert stats["chunks"] == 2
        file_name, file_size, uploaded_at, uploaded_by = conn.db.execute(
            "SELECT file_name, file_size, uploaded_at, uploaded_by FROM conf.documents"
        ).fetchone()
        assert (file_name, file_size, uploaded_by) == ("test.pdf", pdf.stat().st_size, "스크립트 적재")
        assert uploaded_at is not None
        segments = conn.db.execute("SELECT text, sen FROM conf.segments ORDER BY seq").fetchall()
        masked = conn.db.execute("SELECT masked_text, speaker_masked, sen FROM conf.chunks_masked ORDER BY seq").fetchall()
        assert len(segments) == len(masked) == 2
        assert "홍길동" not in masked[0][0]
        assert "홍길동" not in masked[0][1]
        assert segments[1] == ("몸이 안 좋습니다.", "건강")
        assert masked[1][0] == "[민감정보]"
        assert masked[1][2] == "건강"
        assert segments[1][0] not in masked[1][0]
        chunks.pop()
        ingest.ingest_document(pdf, doc, model=(None, None), counts={}, conn=conn)
        assert conn.db.execute("SELECT count(*) FROM conf.segments").fetchone()[0] == 1
        assert conn.db.execute("SELECT count(*) FROM conf.chunks_masked").fetchone()[0] == 1
    finally:
        conn.db.close()


@pytest.mark.parametrize("sensitive_type", ["건강", "노조"])
def test_sensitive_chunk_does_not_expose_original(monkeypatch, sensitive_type):
    text = "이 내용은 민감한 원문입니다."
    monkeypatch.setattr(
        conf_mask, "detect_text",
        lambda *_args: {"entities": [], "og": [], "pos": [], "qi": [], "sen": sensitive_type},
    )
    result = conf_mask.mask_chunk({"chunk_id": "test-1", "text": text}, None, None, {})
    assert result["masked"] == "[민감정보]"
    assert result["sen"] == sensitive_type
    assert text not in result["masked"]


@pytest.mark.parametrize("count", [1, 100])
def test_discipline_subject_department_is_always_masked(monkeypatch, count):
    monkeypatch.setattr(
        conf_mask, "detect_text",
        lambda *_args: {"entities": [], "og": [], "pos": [], "qi": [], "sen": None},
    )
    chunk = {"chunk_id": "test-1", "chunk_type": "항목", "article": "징계대상자 소속", "text": "가상운영팀"}
    result = conf_mask.mask_chunk(chunk, None, None, {("가상운영팀", "과장"): count})
    assert result["masked"] == "[부서]"


@pytest.mark.parametrize(("chunk_type", "article"), [
    ("항목", "소속"),
    ("발언", "징계대상자 소속"),
    ("발언", "발언 1"),
])
def test_other_department_text_keeps_existing_masking(monkeypatch, chunk_type, article):
    monkeypatch.setattr(
        conf_mask, "detect_text",
        lambda *_args: {"entities": [], "og": [], "pos": [], "qi": [], "sen": None},
    )
    chunk = {"chunk_id": "test-1", "chunk_type": chunk_type, "article": article, "text": "가상운영팀"}
    assert conf_mask.mask_chunk(chunk, None, None, {})["masked"] == "가상운영팀"


def test_sensitive_subject_department_keeps_sensitive_mask(monkeypatch):
    monkeypatch.setattr(
        conf_mask, "detect_text",
        lambda *_args: {"entities": [], "og": [], "pos": [], "qi": [], "sen": "건강"},
    )
    chunk = {"chunk_id": "test-1", "chunk_type": "항목", "article": "징계대상자 소속", "text": "민감 내용"}
    assert conf_mask.mask_chunk(chunk, None, None, {})["masked"] == "[민감정보]"


def test_empty_pdf_does_not_overwrite_existing_document(tmp_path, monkeypatch, doc):
    pdf = tmp_path / "test.pdf"
    pdf.touch()
    monkeypatch.setattr(conf_build, "build_doc", lambda *_args: [])
    conn = SQLiteConnection()
    try:
        with pytest.raises(ValueError, match="조각"):
            ingest.ingest_document(pdf, doc, model=(None, None), counts={}, conn=conn)
        assert conn.db.execute("SELECT count(*) FROM conf.documents").fetchone()[0] == 0
    finally:
        conn.db.close()


def test_upload_title_does_not_change_original_title_used_for_parsing(tmp_path, monkeypatch, doc):
    pdf = tmp_path / "test.pdf"
    pdf.touch()
    doc.update(name="화면 제목", _parse_name="원본 제목")
    seen = []
    monkeypatch.setattr(conf_build, "build_doc", lambda meta, _dir: seen.append(meta["name"]) or [])
    with pytest.raises(ValueError, match="조각"):
        ingest.prepare_document(pdf, doc, (None, None), {})
    assert seen == ["원본 제목"]


def test_backfill_uses_pdf_size_and_preserves_existing_values(tmp_path):
    db = sqlite3.connect(":memory:")
    db.execute("ATTACH DATABASE ':memory:' AS conf")
    db.execute("""CREATE TABLE conf.documents (
        doc_id TEXT PRIMARY KEY, source_file TEXT, created_at TEXT, file_name TEXT,
        file_size INTEGER, uploaded_at TEXT, uploaded_by TEXT)""")
    db.executemany("INSERT INTO conf.documents (doc_id, source_file, created_at) VALUES (?, ?, ?)", [
        ("present", "present.pdf", "2026-01-01"), ("missing", "missing.pdf", "2026-01-02"),
        ("partial", "present.pdf", "2026-01-03"),
    ])
    db.execute("UPDATE conf.documents SET file_size = 123, uploaded_by = 'P-test' WHERE doc_id = 'partial'")
    db.commit()
    (tmp_path / "present.pdf").write_bytes(b"%PDF example")

    class Connection:
        def transaction(self):
            return db

        def execute(self, sql, params=()):
            return db.execute(sql.replace("%s", "?").replace("FOR UPDATE", ""), params)

    conn = Connection()
    try:
        assert backfill_conf_upload.backfill(conn, tmp_path) == 3
        rows = db.execute(
            "SELECT doc_id, file_name, file_size, uploaded_at, uploaded_by FROM conf.documents ORDER BY doc_id"
        ).fetchall()
        assert rows == [
            ("missing", "missing.pdf", 0, "2026-01-02", "스크립트 적재"),
            ("partial", "present.pdf", 123, "2026-01-03", "P-test"),
            ("present", "present.pdf", 12, "2026-01-01", "스크립트 적재"),
        ]
        assert backfill_conf_upload.backfill(conn, tmp_path) == 0
    finally:
        db.close()


def test_leak_check_reports_entity_and_sensitive_leaks(monkeypatch):
    monkeypatch.setattr(check_ingest, "CONF", {"documents": [{"id": "test-document"}]})

    class Rows:
        def __init__(self, value):
            self.value = value

        def fetchall(self):
            return self.value

    class FakeConnection:
        def execute(self, sql):
            if "FROM conf.documents d" in sql:
                return Rows([{"doc_id": "test-document", "status": "ready", "chunk_count": 1,
                              "segments": 1, "masked": 1}])
            return Rows([{"chunk_id": "test-1", "doc_id": "test-document",
                          "entities": [{"text": "홍길동"}], "sen": "건강",
                          "masked_text": "홍길동의 민감한 내용", "speaker_masked": None}])

    _, _, failures = check_ingest.check(FakeConnection())
    assert ("test-1", "entity remains in masked copy") in failures
    assert ("test-1", "sensitive text is not fully masked") in failures
