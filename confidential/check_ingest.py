"""적재된 기밀문서의 조각 수와 가린 사본을 읽기 전용으로 확인합니다."""
import argparse
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from app import config
from .conf_build import CONF


def check(conn, names=()):
    failures = []
    documents = conn.execute(
        """SELECT d.doc_id, d.status, d.chunk_count,
           (SELECT count(*) FROM conf.segments s WHERE s.doc_id=d.doc_id) AS segments,
           (SELECT count(*) FROM conf.chunks_masked m WHERE m.doc_id=d.doc_id) AS masked
           FROM conf.documents d ORDER BY d.doc_id"""
    ).fetchall()
    expected = {d["id"] for d in CONF["documents"]}
    actual = {d["doc_id"] for d in documents}
    for doc_id in sorted(expected - actual):
        failures.append((doc_id, "missing document"))
    for doc_id in sorted(actual - expected):
        failures.append((doc_id, "unexpected document"))
    for doc in documents:
        if doc["status"] != "ready" or doc["chunk_count"] != doc["segments"] or doc["segments"] != doc["masked"]:
            failures.append((doc["doc_id"], "status or chunk count mismatch"))

    chunks = conn.execute(
        """SELECT s.chunk_id, s.doc_id, s.entities, s.sen,
           m.masked_text, m.speaker_masked
           FROM conf.segments s JOIN conf.chunks_masked m ON m.chunk_id=s.chunk_id
           ORDER BY s.doc_id, s.seq"""
    ).fetchall()
    for chunk in chunks:
        for entity in chunk["entities"]:
            value = entity["text"]
            if value and (value in chunk["masked_text"] or value in (chunk["speaker_masked"] or "")):
                failures.append((chunk["chunk_id"], "entity remains in masked copy"))
        if chunk["sen"] and chunk["masked_text"] != "[민감정보]":
            failures.append((chunk["chunk_id"], "sensitive text is not fully masked"))
        if any(name in chunk["masked_text"] or name in (chunk["speaker_masked"] or "") for name in names):
            failures.append((chunk["chunk_id"], "name list match"))
    return documents, chunks, failures


def main(argv=None):
    parser = argparse.ArgumentParser(description="기밀문서 적재 결과 확인")
    parser.add_argument("--names", type=Path, help="저장소 밖 이름 목록 파일")
    args = parser.parse_args(argv)
    names = []
    if args.names:
        names = [line.strip() for line in args.names.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        if not names:
            parser.error("이름 목록이 비어 있습니다")
    if not config.HRDB_URL:
        parser.error("HRDB_URL이 필요합니다")
    with psycopg.connect(config.HRDB_URL, row_factory=dict_row, connect_timeout=5) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            documents, chunks, failures = check(conn, names)
    print("doc_id | status | declared | segments | masked")
    for doc in documents:
        print(f"{doc['doc_id']} | {doc['status']} | {doc['chunk_count']} | {doc['segments']} | {doc['masked']}")
    if failures:
        for item, reason in failures:
            print(f"FAIL | {item} | {reason}")
        return 1
    print("누출 검사: 통과")
    for doc in documents:
        print(f"\n{doc['doc_id']} 가린 예시:")
        examples = [c["masked_text"] for c in chunks if c["doc_id"] == doc["doc_id"]][:3]
        for example in examples:
            print(example.replace("\n", " "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
