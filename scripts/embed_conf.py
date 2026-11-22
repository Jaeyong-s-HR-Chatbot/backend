"""가린 기밀문서 조각의 누락 임베딩을 채웁니다."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg

from app import config
from confidential import embedding


def main(argv=None):
    parser = argparse.ArgumentParser(description="기밀문서 가린 조각 임베딩")
    parser.add_argument("--dry-run", action="store_true", help="대상 건수만 읽기 전용으로 확인")
    args = parser.parse_args(argv)
    if not config.HRDB_URL:
        raise RuntimeError("HRDB_URL이 필요합니다")
    with psycopg.connect(config.HRDB_URL, connect_timeout=5) as conn:
        if args.dry_run:
            with conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")
                count = conn.execute(
                    "SELECT count(*) FROM conf.chunks_masked WHERE embedding IS NULL"
                ).fetchone()[0]
            print(f"임베딩 대상: {count}건")
            return
        with conn.transaction():
            rows = conn.execute(
                "SELECT chunk_id, masked_text FROM conf.chunks_masked WHERE embedding IS NULL ORDER BY chunk_id FOR UPDATE"
            ).fetchall()
            if not rows:
                print("임베딩 대상: 0건")
                return
            vectors = embedding.encode([row[1] for row in rows])
            if vectors is None:
                raise RuntimeError("임베딩 모델을 사용할 수 없습니다")
            for (chunk_id, _), vector in zip(rows, vectors):
                conn.execute(
                    "UPDATE conf.chunks_masked SET embedding = %s::vector WHERE chunk_id = %s AND embedding IS NULL",
                    (embedding.literal(vector), chunk_id),
                )
        print(f"임베딩 완료: {len(rows)}건")


if __name__ == "__main__":
    main()
