"""기밀문서 목록과 업로드 상태의 DB 저장."""
from contextlib import contextmanager
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from . import config
from confidential import ingest
from confidential.upload_meta import CATEGORY_ROLES


class BusyDocument(Exception):
    pass


class ForbiddenDocument(Exception):
    pass


@contextmanager
def connection():
    if not config.HRDB_URL:
        raise RuntimeError("기밀문서 DB가 설정되지 않았습니다")
    with psycopg.connect(config.HRDB_URL, row_factory=dict_row, connect_timeout=5) as conn:
        yield conn


def _lock(conn, doc_id):
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (doc_id,))


def start_document(meta, roles, temporary_path, final_path):
    backup = final_path.with_name(final_path.name + "." + uuid4().hex + ".tmp")
    saved = replaced = committed = False
    try:
        with connection() as conn:
            with conn.transaction():
                _lock(conn, meta["id"])
                old = conn.execute(
                    "SELECT category, status FROM conf.documents WHERE doc_id = %s FOR UPDATE",
                    (meta["id"],),
                ).fetchone()
                if old and CATEGORY_ROLES.get(old["category"]) not in roles:
                    raise ForbiddenDocument()
                if old and old["status"] == "processing":
                    raise BusyDocument()
                ingest.upsert_document(conn, meta, "processing", 0)
                if final_path.exists():
                    final_path.replace(backup)
                    saved = True
                temporary_path.replace(final_path)
                replaced = True
            committed = True
    finally:
        if not committed:
            if replaced:
                final_path.unlink(missing_ok=True)
            if saved:
                backup.replace(final_path)
        elif saved:
            backup.unlink()


def list_documents(roles):
    categories = [category for category, role in CATEGORY_ROLES.items() if role in roles]
    with connection() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        return conn.execute(
            """SELECT doc_id, title, confidential, doc_no, owner_dept, issued_at,
                file_name, file_size, uploaded_at, uploaded_by, source_file, created_at, status
               FROM conf.documents
               WHERE %s OR category = ANY(%s)
               ORDER BY COALESCE(uploaded_at, created_at) DESC, doc_id""",
            ("SYS_ADMIN" in roles, categories),
        ).fetchall()


@contextmanager
def read_document(doc_id):
    with connection() as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            doc = conn.execute(
                """SELECT doc_id, title, confidential, doc_no, owner_dept, issued_at, status,
                    doc_kind, category, level, allowed_roles, subject_emp_id, participants
                    FROM conf.documents WHERE doc_id = %s""",
                (doc_id,),
            ).fetchone()

            def segments():
                return conn.execute(
                    """SELECT seq, chunk_type, article, speaker_label, text, level,
                        entities, og, pos, qi, sen
                        FROM conf.segments WHERE doc_id = %s ORDER BY seq""",
                    (doc_id,),
                ).fetchall()

            yield doc, segments


def log_view(user_pseudo_id, doc_id, mode):
    with connection() as conn:
        conn.execute(
            "INSERT INTO app.conf_view_log (user_pseudo_id, doc_id, mode) VALUES (%s, %s, %s)",
            (user_pseudo_id, doc_id, mode),
        )


def mark_failed(doc_id):
    with connection() as conn:
        with conn.transaction():
            _lock(conn, doc_id)
            conn.execute(
                "UPDATE conf.documents SET status = 'failed' WHERE doc_id = %s AND status = 'processing'",
                (doc_id,),
            )


def delete_document(doc_id, roles, path):
    backup = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    saved = committed = False
    try:
        with connection() as conn:
            with conn.transaction():
                _lock(conn, doc_id)
                row = conn.execute(
                    "SELECT category, status FROM conf.documents WHERE doc_id = %s FOR UPDATE",
                    (doc_id,),
                ).fetchone()
                if not row:
                    return False
                if CATEGORY_ROLES.get(row["category"]) not in roles:
                    raise ForbiddenDocument()
                if row["status"] == "processing":
                    raise BusyDocument()
                conn.execute("DELETE FROM conf.chunks_masked WHERE doc_id = %s", (doc_id,))
                conn.execute("DELETE FROM conf.segments WHERE doc_id = %s", (doc_id,))
                conn.execute("DELETE FROM conf.documents WHERE doc_id = %s", (doc_id,))
                if path.exists():
                    path.replace(backup)
                    saved = True
            committed = True
    finally:
        if saved:
            if committed:
                backup.unlink()
            else:
                backup.replace(path)
    return True
