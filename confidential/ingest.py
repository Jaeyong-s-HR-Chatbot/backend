"""기밀문서 PDF를 원본 조각과 가린 사본으로 분리해 적재합니다."""
import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from app import config
from . import conf_build, conf_mask, ner_model

HERE = Path(__file__).parent


def load_counts():
    if not config.HRDB_URL:
        raise RuntimeError("부서·직급 인원 조회에 HRDB_URL이 필요합니다")
    sql = (HERE / "rules" / "org_counts.sql").read_text(encoding="utf-8")
    with psycopg.connect(config.HRDB_URL, connect_timeout=5) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            return {(dept, position): count for dept, position, count in conn.execute(sql).fetchall()}


def _entity_rows(masked):
    result = []
    for field, key in (("text", "entities"), ("speaker_label", "speaker_entities")):
        for kind, start, end, value in masked.get(key, []):
            result.append({"field": field, "type": kind, "start": start, "end": end, "text": value})
    return result


def prepare_document(pdf_path, meta, model, counts):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", meta["id"]):
        raise ValueError("문서 ID 형식이 올바르지 않습니다")
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        raise FileNotFoundError("지정한 PDF를 찾을 수 없습니다")
    doc = dict(meta, file=pdf_path.name)
    parse_doc = dict(doc, name=doc.get("_parse_name", doc["name"]))
    chunks = conf_build.build_doc(parse_doc, pdf_path.parent)
    if not chunks:
        raise ValueError("PDF에서 조각을 찾지 못해 적재를 중단합니다")
    tok, ner = model
    pairs = [(chunk, conf_mask.mask_chunk(chunk, tok, ner, counts)) for chunk in chunks]
    return doc, pairs


def upsert_document(conn, meta, status, chunk_count):
    upload = meta["_upload"]
    return conn.execute(
        """INSERT INTO conf.documents
        (doc_id, title, doc_kind, form, category, level, allowed_roles, subject_emp_id,
         participants, pair_doc_id, issued_at, owner_dept, doc_no, confidential,
         source_file, status, chunk_count, file_name, file_size, uploaded_at, uploaded_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (doc_id) DO UPDATE SET
          title=EXCLUDED.title, doc_kind=EXCLUDED.doc_kind, form=EXCLUDED.form,
          category=EXCLUDED.category, level=EXCLUDED.level, allowed_roles=EXCLUDED.allowed_roles,
          subject_emp_id=EXCLUDED.subject_emp_id, participants=EXCLUDED.participants,
          pair_doc_id=EXCLUDED.pair_doc_id, issued_at=EXCLUDED.issued_at,
          owner_dept=EXCLUDED.owner_dept, doc_no=EXCLUDED.doc_no,
          confidential=EXCLUDED.confidential, source_file=EXCLUDED.source_file,
          status=EXCLUDED.status, chunk_count=EXCLUDED.chunk_count,
          file_name=EXCLUDED.file_name, file_size=EXCLUDED.file_size,
          uploaded_at=EXCLUDED.uploaded_at, uploaded_by=EXCLUDED.uploaded_by""",
        (meta["id"], meta["name"], meta["doc_kind"], meta["form"], meta["category"],
         meta["level"], meta.get("allowed_roles", []), meta.get("subject"),
         Jsonb(meta.get("participants", [])), meta.get("pair"), meta.get("date"),
         meta.get("owner_dept"), meta.get("doc_no"), meta.get("confidential", True),
         meta["file"], status, chunk_count, upload["file_name"], upload["file_size"],
         upload["uploaded_at"], upload["uploaded_by"]),
    )


def write_document(conn, meta, pairs):
    with conn.transaction():
        upsert_document(conn, meta, "processing", len(pairs))
        conn.execute("DELETE FROM conf.chunks_masked WHERE doc_id = %s", (meta["id"],))
        conn.execute("DELETE FROM conf.segments WHERE doc_id = %s", (meta["id"],))
        for chunk, masked in pairs:
            seq = int(chunk["chunk_id"].rsplit("-", 1)[1])
            conn.execute(
                """INSERT INTO conf.segments
                (chunk_id, doc_id, seq, chunk_type, article, page, speaker_code, speaker_label,
                 text, level, entities, og, pos, qi, sen)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (chunk["chunk_id"], meta["id"], seq, chunk["chunk_type"], chunk["article"],
                 chunk["page"], chunk.get("speaker"), chunk.get("speaker_label"), chunk["text"],
                 meta["level"], Jsonb(_entity_rows(masked)), Jsonb(masked["og"]),
                 Jsonb(masked["pos"]), Jsonb(masked["qi"]), masked["sen"]),
            )
            conn.execute(
                """INSERT INTO conf.chunks_masked
                (chunk_id, doc_id, seq, chunk_type, article, speaker_masked, masked_text,
                 level, sen, allowed_roles, embedding, tsv)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, to_tsvector('simple', %s))""",
                (chunk["chunk_id"], meta["id"], seq, chunk["chunk_type"], chunk["article"],
                 masked.get("speaker_masked"), masked["masked"], meta["level"], masked["sen"],
                 meta.get("allowed_roles", []), masked["masked"]),
            )
        conn.execute("UPDATE conf.documents SET status = 'ready' WHERE doc_id = %s", (meta["id"],))


def ingest_document(pdf_path, meta, *, model=None, counts=None, conn=None, dry_run=False):
    """한 문서를 처리합니다. 외부 호출자는 기존 연결과 모델을 재사용할 수 있습니다."""
    counts = load_counts() if counts is None else counts
    model = ner_model.load() if model is None else model
    doc, pairs = prepare_document(pdf_path, meta, model, counts)
    doc["file"] = meta["file"]
    if "_upload" not in doc:
        doc["_upload"] = {
            "file_name": Path(pdf_path).name,
            "file_size": Path(pdf_path).stat().st_size,
            "uploaded_at": datetime.now(timezone.utc),
            "uploaded_by": "스크립트 적재",
        }
    if dry_run:
        conf_build.WORK_DIR.mkdir(parents=True, exist_ok=True)
        path = conf_build.WORK_DIR / (doc["id"] + ".jsonl")
        with path.open("w", encoding="utf-8") as f:
            for chunk, masked in pairs:
                f.write(json.dumps({"segment": chunk, "masked": masked}, ensure_ascii=False) + "\n")
    elif conn is not None:
        write_document(conn, doc, pairs)
    else:
        if not config.HRDB_URL:
            raise RuntimeError("DB 적재에 HRDB_URL이 필요합니다")
        with psycopg.connect(config.HRDB_URL, connect_timeout=5) as connection:
            write_document(connection, doc, pairs)
    return {
        "doc_id": doc["id"],
        "chunks": len(pairs),
        "entities": sum(len(m["entities"]) + len(m.get("speaker_entities", [])) for _, m in pairs),
        "qi_masked": sum(sum(bool(q[3]) for q in m["qi"]) for _, m in pairs),
        "sensitive": sum(bool(m["sen"]) for _, m in pairs),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="기밀문서 원본과 가린 사본 적재")
    parser.add_argument("--dry-run", action="store_true", help="DB 쓰기 없이 중간 결과만 저장")
    parser.add_argument("--doc", help="처리할 문서 ID")
    args = parser.parse_args(argv)
    documents = conf_build.CONF["documents"]
    if args.doc:
        documents = [d for d in documents if d["id"] == args.doc]
        if not documents:
            parser.error("문서 ID를 찾지 못했습니다")
    counts = load_counts()
    model = ner_model.load()
    for doc in documents:
        stats = ingest_document(conf_build.RAW / doc["file"], doc, model=model, counts=counts, dry_run=args.dry_run)
        print(f"{stats['doc_id']} | chunks={stats['chunks']} | entities={stats['entities']} "
              f"| qi_masked={stats['qi_masked']} | sensitive={stats['sensitive']}")


if __name__ == "__main__":
    main()
