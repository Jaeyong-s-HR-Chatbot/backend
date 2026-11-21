"""등록된 문서의 업로드 머리말을 결정합니다."""
import re
from datetime import date

from .conf_build import CONF

UNREGISTERED = "문서 정보가 등록되지 않은 파일이에요 (대상자·위원 입력 칸 추가 후 지원)"
PUBLIC_UNSUPPORTED = "일반 문서 업로드는 아직 지원하지 않아요"
CATEGORY_ROLES = {"징계": "HR_DISCIPLINE", "노사": "HR_LABOR"}


def resolve(filename, *, confidential, title, doc_no, owner_dept, issued_at):
    if not confidential:
        raise ValueError(PUBLIC_UNSUPPORTED)
    candidates = [doc for doc in CONF["documents"] if doc["file"] == filename]
    if len(candidates) != 1:
        raise ValueError(UNREGISTERED)
    doc = dict(candidates[0])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", doc["id"]):
        raise ValueError("문서 ID 형식이 올바르지 않습니다")
    if doc["category"] not in CATEGORY_ROLES:
        raise ValueError("지원하지 않는 문서 분류입니다")
    try:
        doc["date"] = date.fromisoformat(issued_at.strip()) if issued_at.strip() else doc.get("date")
    except ValueError:
        raise ValueError("시행일 형식이 올바르지 않습니다") from None
    doc["_parse_name"] = doc["name"]
    doc["name"] = title.strip() or doc["name"]
    doc["doc_no"] = doc_no.strip() or doc.get("doc_no")
    doc["owner_dept"] = owner_dept.strip() or doc.get("owner_dept")
    doc["confidential"] = True
    return doc
