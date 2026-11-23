"""기존 스크립트 적재 문서의 업로드 메타데이터를 채웁니다."""
import psycopg

from app import config
from confidential import conf_build


def backfill(conn, pdf_dir):
    with conn.transaction():
        rows = conn.execute(
            """SELECT doc_id, source_file FROM conf.documents
            WHERE file_name IS NULL OR file_size IS NULL
               OR uploaded_at IS NULL OR uploaded_by IS NULL
            FOR UPDATE"""
        ).fetchall()
        for doc_id, source_file in rows:
            path = (pdf_dir / source_file).resolve()
            if path.parent != pdf_dir.resolve():
                raise ValueError("원본 파일명 형식이 올바르지 않습니다")
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                size = 0
            conn.execute(
                """UPDATE conf.documents SET
                    file_name = COALESCE(file_name, source_file),
                    file_size = COALESCE(file_size, %s),
                    uploaded_at = COALESCE(uploaded_at, created_at),
                    uploaded_by = COALESCE(uploaded_by, %s)
                WHERE doc_id = %s""",
                (size, "스크립트 적재", doc_id),
            )
    return len(rows)


def main():
    if not config.HRDB_URL:
        raise RuntimeError("HRDB_URL이 필요합니다")
    with psycopg.connect(config.HRDB_URL, connect_timeout=5) as conn:
        count = backfill(conn, conf_build.RAW)
    print(f"메타데이터 확인 문서: {count}건")


if __name__ == "__main__":
    main()
