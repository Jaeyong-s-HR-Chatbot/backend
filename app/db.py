"""DB 연결. HRDB_URL이 있을 때만 쓰여요. 조회는 모두 읽기 전용 트랜잭션으로 실행해요."""
from contextlib import contextmanager

from . import config


@contextmanager
def connect():
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(config.HRDB_URL, row_factory=dict_row, connect_timeout=5) as con:
        con.execute("SET TRANSACTION READ ONLY")
        yield con


def fetch_all(sql: str, params: tuple = ()) -> list[dict]:
    with connect() as con:
        return con.execute(sql, params).fetchall()


def fetch_one(sql: str, params: tuple = ()) -> dict | None:
    rows = fetch_all(sql, params)
    return rows[0] if rows else None


def insert_chat_log(
    *,
    user_pseudo_id: str,
    question_masked: str,
    answer_masked: str | None,
    pii_found: dict,
    intent: str | None,
    kind: str | None,
    cited_chunks: list[str],
    tools_used: list[str],
    latency_ms: int | None,
) -> None:
    """app.chat_log에만 대화 마스킹 로그를 쓴다."""
    import psycopg
    from psycopg.types.json import Jsonb

    with psycopg.connect(config.HRDB_URL, connect_timeout=5) as con:
        con.execute(
            "INSERT INTO app.chat_log "
            "(user_pseudo_id, question_masked, answer_masked, pii_found, intent, kind, "
            "cited_chunks, tools_used, latency_ms) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                user_pseudo_id,
                question_masked,
                answer_masked,
                Jsonb(pii_found),
                intent,
                kind,
                Jsonb(cited_chunks),
                Jsonb(tools_used),
                latency_ms,
            ),
        )


def ping() -> dict:
    """/health 용: 연결되는지와 기본 건수"""
    try:
        r = fetch_one("SELECT (SELECT count(*) FROM hr.employees) AS employees, "
                      "(SELECT count(*) FROM rag.chunks WHERE searchable) AS chunks, "
                      "(SELECT count(embedding) FROM rag.chunks) AS embedded")
        return {"ok": True, **r}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
