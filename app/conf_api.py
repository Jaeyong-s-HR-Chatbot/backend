"""기밀문서 업로드, 목록 및 삭제 API."""
import logging
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import psycopg
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.formparsers import MultiPartException, MultiPartParser

from . import conf_access, conf_store, roles
from .auth import current_user
from .masking import pseudonymize
from .schemas import Me
from confidential import ingest, ner_model, upload_meta

log = logging.getLogger(__name__)
UPLOAD_DIR = Path(__file__).resolve().parents[1] / "data" / "conf" / "uploads"
MAX_SIZE = 50 * 1024 * 1024
_model = None
_model_lock = threading.Lock()


class LocalMultipartParser(MultiPartParser):
    """Starlette의 임시 파일도 기밀문서 작업 폴더에만 저장합니다."""

    def on_part_begin(self):
        super().on_part_begin()
        self._file_size = 0

    def on_headers_finished(self):
        super().on_headers_finished()
        if self._current_part.file is not None:
            original = self._current_part.file.file
            local = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, dir=UPLOAD_DIR)
            self._current_part.file.file = local
            self._files_to_close_on_error.append(local)
            original.close()

    def on_part_data(self, data, start, end):
        if self._current_part.file is not None:
            self._file_size += end - start
            if self._file_size > MAX_SIZE:
                raise MultiPartException("파일은 50MB 이하여야 합니다")
        super().on_part_data(data, start, end)


class LocalUploadRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def route_handler(request: Request):
            if request.method == "POST":
                try:
                    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                    parser = LocalMultipartParser(request.headers, request.stream(), max_files=1, max_fields=10)
                    request._form = await parser.parse()
                except MultiPartException as exc:
                    raise HTTPException(400, str(exc)) from None
                except (OSError, KeyError) as exc:
                    log.error("action=upload_parse_error error=%s", type(exc).__name__)
                    raise HTTPException(400, "업로드 요청을 처리할 수 없습니다") from None
            return await handler(request)

        return route_handler


router = APIRouter(route_class=LocalUploadRoute)


class DocumentMeta(BaseModel):
    doc_id: str
    title: str
    confidential: bool
    doc_no: str
    owner_dept: str
    issued_at: str
    file_name: str
    file_size: int
    uploaded_at: str
    uploaded_by: str
    status: Literal["processing", "ready", "failed"]


class ViewText(BaseModel):
    text: str


class ViewRedaction(BaseModel):
    redacted: Literal[True]
    level: Literal[1, 2, 3, 4]


class ViewBlock(BaseModel):
    type: Literal["p"] = "p"
    segments: list[ViewText | ViewRedaction]


class ViewSection(BaseModel):
    heading: str
    blocks: list[ViewBlock]


class DocumentView(BaseModel):
    doc_id: str
    title: str
    confidential: bool
    doc_no: str
    owner_dept: str
    issued_at: str
    view: Literal["original", "redacted"]
    redacted_count: int
    sections: list[ViewSection]


SAFE_HEADINGS = {
    "제목", "본문", "서명 위원", "서명 위원장", "근로자위원", "사용자위원", "출석위원수",
    "징계대상자 성명", "징계대상자 생년월일", "징계대상자 소속",
    *(label for form in upload_meta.CONF["forms"].values() for label in form["labels"]),
}


def _sections(doc, segments, mode):
    sections = {}
    is_transcript = doc["doc_kind"] == "녹취록"
    for segment in segments:
        if is_transcript:
            heading = "발언" if segment["chunk_type"] == "발언" else "회의 정보"
        else:
            article = segment.get("article")
            heading = article if mode == "full" or article in SAFE_HEADINGS else "기타"
            heading = heading or "기타"
        item = dict(segment, level=segment["level"] if segment.get("review_required") else doc["level"])
        rendered = conf_access.segment_view(mode, item)
        if heading not in sections:
            sections[heading] = ViewSection(heading=heading, blocks=[])
        sections[heading].blocks.append(ViewBlock(segments=rendered))
    return list(sections.values())


def _iso(value):
    return value.isoformat() if value is not None else ""


def _document(row):
    return DocumentMeta(
        doc_id=row["doc_id"], title=row["title"], confidential=row["confidential"],
        doc_no=row["doc_no"] or "", owner_dept=row["owner_dept"] or "",
        issued_at=_iso(row["issued_at"]), file_name=row["file_name"] or row["source_file"],
        file_size=row["file_size"] if row["file_size"] is not None else 0,
        uploaded_at=_iso(row["uploaded_at"] or row["created_at"]),
        uploaded_by=row["uploaded_by"] or "스크립트 적재", status=row["status"],
    )


def _access(user):
    return roles.get_roles(user.employee_id)


def _load_model():
    global _model
    with _model_lock:
        if _model is None:
            _model = ner_model.load()
        return _model


def _process(doc, path, actor):
    try:
        ingest.ingest_document(path, doc, model=_load_model())
        log.info("action=upload_ready actor=%s doc_id=%s", actor, doc["id"])
    except Exception as exc:
        log.error("action=upload_failed actor=%s doc_id=%s error=%s", actor, doc["id"], type(exc).__name__)
        try:
            conf_store.mark_failed(doc["id"])
        except (psycopg.Error, OSError, RuntimeError) as mark_error:
            log.error("action=mark_failed_error actor=%s doc_id=%s error=%s",
                      actor, doc["id"], type(mark_error).__name__)


def _store_error(exc):
    if isinstance(exc, conf_store.ForbiddenDocument):
        return HTTPException(403, "문서 처리 권한이 없습니다")
    if isinstance(exc, conf_store.BusyDocument):
        return HTTPException(409, "문서가 처리 중입니다")
    log.error("action=store_error error=%s", type(exc).__name__)
    return HTTPException(503, "기밀문서 저장소를 사용할 수 없습니다")


@router.get("/documents", response_model=list[DocumentMeta])
def list_documents(user: Me = Depends(current_user)):
    access = _access(user)
    if not access.intersection({"HR_DISCIPLINE", "HR_LABOR", "SYS_ADMIN"}):
        raise HTTPException(403, "문서 목록 권한이 없습니다")
    try:
        return [_document(row) for row in conf_store.list_documents(access)]
    except (psycopg.Error, OSError, RuntimeError) as exc:
        raise _store_error(exc) from None


@router.get("/documents/{doc_id}", response_model=DocumentView)
def view_document(doc_id: str, user: Me = Depends(current_user)):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", doc_id):
        raise HTTPException(404, "문서를 찾을 수 없습니다")
    access_roles = _access(user)
    try:
        with conf_store.read_document(doc_id) as (doc, read_segments):
            if doc is None:
                raise HTTPException(404, "문서를 찾을 수 없습니다")
            mode = conf_access.access(user.employee_id, access_roles, doc)
            if mode == "deny":
                raise HTTPException(403, "열람 권한이 없어요")
            if doc["status"] != "ready":
                raise HTTPException(409, "문서가 준비되지 않았습니다")
            segments = read_segments()
            if not segments:
                raise ValueError("문서 조각이 없습니다")
            sections = _sections(doc, segments, mode)
            count = sum(isinstance(part, ViewRedaction)
                        for section in sections for block in section.blocks for part in block.segments)
            response = DocumentView(
                doc_id=doc["doc_id"], title=doc["title"], confidential=doc["confidential"],
                doc_no=doc["doc_no"] or "", owner_dept=doc["owner_dept"] or "",
                issued_at=_iso(doc["issued_at"]), view="redacted" if count else "original",
                redacted_count=count, sections=sections,
            )
    except (psycopg.Error, OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        log.error("action=view_error actor=%s doc_id=%s error=%s",
                  pseudonymize(user.employee_id), doc_id, type(exc).__name__)
        raise HTTPException(503, "문서를 안전하게 처리할 수 없습니다") from None
    if mode == "full" and doc["level"] == "L4":
        # TODO: L4 상세 열람 사유 입력.
        try:
            conf_store.log_view(pseudonymize(user.employee_id), doc_id, mode)
        except (psycopg.Error, OSError, RuntimeError) as exc:
            log.warning("action=view_log_failed actor=%s doc_id=%s error=%s",
                        pseudonymize(user.employee_id), doc_id, type(exc).__name__)
    return response


@router.post("/documents", response_model=DocumentMeta)
def upload_document(
    background: BackgroundTasks, file: UploadFile = File(...),
    title: str = Form(""), confidential: bool = Form(...), doc_no: str = Form(""),
    owner_dept: str = Form(""), issued_at: str = Form(""), user: Me = Depends(current_user),
):
    access = _access(user)
    if not access.intersection({"HR_DISCIPLINE", "HR_LABOR"}):
        raise HTTPException(403, "문서 업로드 권한이 없습니다")
    filename = file.filename or ""
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(400, "PDF 파일만 업로드할 수 있습니다")
    try:
        doc = upload_meta.resolve(filename, confidential=confidential, title=title, doc_no=doc_no,
                                  owner_dept=owner_dept, issued_at=issued_at)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if upload_meta.CATEGORY_ROLES[doc["category"]] not in access:
        raise HTTPException(403, "문서 업로드 권한이 없습니다")
    actor = pseudonymize(user.employee_id)
    temp_path = None
    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=UPLOAD_DIR, suffix=".tmp", delete=False) as temp:
            temp_path = Path(temp.name)
            first = file.file.read(1024 * 1024)
            if not first.startswith(b"%PDF"):
                raise HTTPException(400, "올바른 PDF 파일이 아닙니다")
            size = len(first)
            temp.write(first)
            while True:
                part = file.file.read(1024 * 1024)
                if not part:
                    break
                size += len(part)
                if size > MAX_SIZE:
                    raise HTTPException(400, "파일은 50MB 이하여야 합니다")
                temp.write(part)
        uploaded_at = datetime.now(timezone.utc)
        doc["_upload"] = {"file_name": filename, "file_size": size,
                          "uploaded_at": uploaded_at, "uploaded_by": actor}
        path = UPLOAD_DIR / (doc["id"] + ".pdf")
        conf_store.start_document(doc, access, temp_path, path)
        background.add_task(_process, doc, path, actor)
        log.info("action=upload actor=%s doc_id=%s", actor, doc["id"])
        return DocumentMeta(
            doc_id=doc["id"], title=doc["name"], confidential=True,
            doc_no=doc.get("doc_no") or "", owner_dept=doc.get("owner_dept") or "",
            issued_at=_iso(doc.get("date")), file_name=filename, file_size=size,
            uploaded_at=_iso(uploaded_at), uploaded_by=actor, status="processing",
        )
    except (conf_store.ForbiddenDocument, conf_store.BusyDocument, psycopg.Error, OSError, RuntimeError) as exc:
        raise _store_error(exc) from None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        file.file.close()


@router.delete("/documents/{doc_id}", status_code=204)
def delete_document(doc_id: str, user: Me = Depends(current_user)):
    access = _access(user)
    if not access.intersection({"HR_DISCIPLINE", "HR_LABOR"}):
        raise HTTPException(403, "문서 삭제 권한이 없습니다")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", doc_id):
        raise HTTPException(404, "문서를 찾을 수 없습니다")
    try:
        deleted = conf_store.delete_document(doc_id, access, UPLOAD_DIR / (doc_id + ".pdf"))
    except (conf_store.ForbiddenDocument, conf_store.BusyDocument, psycopg.Error, OSError, RuntimeError) as exc:
        raise _store_error(exc) from None
    if not deleted:
        raise HTTPException(404, "문서를 찾을 수 없습니다")
    log.info("action=delete actor=%s doc_id=%s", pseudonymize(user.employee_id), doc_id)
