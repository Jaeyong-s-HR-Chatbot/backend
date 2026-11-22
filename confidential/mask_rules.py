"""적재와 열람에서 공유하는 양식 항목 가림 규칙."""
SUBJECT_DEPARTMENT = "징계대상자 소속"


def is_subject_department(chunk):
    return chunk.get("chunk_type") == "항목" and chunk.get("article") == SUBJECT_DEPARTMENT
