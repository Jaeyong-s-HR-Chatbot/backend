"""문서 머리말과 탐지 구간으로 기밀문서 열람을 판단합니다."""
from typing import Literal

from confidential.upload_meta import CATEGORY_ROLES
from confidential.mask_rules import is_subject_department

AccessMode = Literal["full", "subject", "masked", "deny"]
PARTICIPANT_ROLES = {"COMMITTEE_CHAIR", "COMMITTEE", "COUNCIL"}
SUBJECT_ARTICLES = {
    "제목", "안건", "징계내용", "의결사항", "회의일자", "회의시간", "회의일시",
    "회의장소", "징계대상자 성명", "징계대상자 생년월일", "징계대상자 소속",
}


def access(emp_id: str, roles: set[str], doc: dict) -> AccessMode:
    level = doc.get("level")
    if (level not in {"L1", "L2", "L3", "L4"} or doc.get("category") not in CATEGORY_ROLES
            or doc.get("doc_kind") not in {"회의록", "녹취록"}):
        return "deny"
    allowed = doc.get("allowed_roles")
    participants = doc.get("participants")
    if not isinstance(allowed, list) or not all(isinstance(role, str) for role in allowed):
        return "deny"
    if not isinstance(participants, list) or not all(isinstance(person, dict) for person in participants):
        return "deny"
    eligible_roles = roles - {"SYS_ADMIN"}
    if "SYS_ADMIN" in roles:
        eligible_roles -= {"EMPLOYEE", "TEAM_LEAD"}
    if CATEGORY_ROLES[doc["category"]] in roles or eligible_roles.intersection(allowed):
        return "full"
    if any(person.get("emp_id") == emp_id and person.get("role") in PARTICIPANT_ROLES
           for person in participants):
        return "full"
    if doc.get("subject_emp_id") == emp_id:
        return "subject" if doc.get("doc_kind") == "회의록" else "deny"
    if "SYS_ADMIN" in roles:
        return "deny"
    return "masked" if level in {"L1", "L2"} else "deny"


def _redacted(level: int) -> list[dict]:
    return [{"redacted": True, "level": level}]


def _position(text: str, start, end, value) -> tuple[int, int]:
    if (type(start) is not int or type(end) is not int or not isinstance(value, str)
            or not value or not 0 <= start < end <= len(text) or text[start:end] != value):
        raise ValueError("유효하지 않은 마스킹 구간")
    return start, end


def _spans(segment: dict, field: str, text: str) -> list[tuple[int, int]]:
    entities = segment["entities"]
    if not isinstance(entities, list):
        raise ValueError("유효하지 않은 탐지 구간")
    spans = []
    for entity in entities:
        if not isinstance(entity, dict) or entity.get("field") not in {"text", "speaker_label"}:
            raise ValueError("유효하지 않은 탐지 필드")
        if entity["field"] == field:
            spans.append(_position(text, entity.get("start"), entity.get("end"), entity.get("text")))
    if field != "text":
        return spans
    og, pos, qi = segment["og"], segment["pos"], segment["qi"]
    if not all(isinstance(value, list) for value in (og, pos, qi)):
        raise ValueError("유효하지 않은 간접 식별 구간")
    for pair in qi:
        if not isinstance(pair, list) or len(pair) != 4 or type(pair[3]) is not bool:
            raise ValueError("유효하지 않은 간접 식별 조합")
        if not pair[3]:
            continue
        for entries, wanted, kind in ((og, pair[0], "OG"), (pos, pair[1], "POS")):
            if not isinstance(wanted, str) or not wanted:
                raise ValueError("유효하지 않은 간접 식별 값")
            matches = []
            for entry in entries:
                if not isinstance(entry, list) or len(entry) != 4:
                    raise ValueError("유효하지 않은 간접 식별 위치")
                if entry[0] == kind and entry[3] == wanted:
                    matches.append(_position(text, entry[1], entry[2], entry[3]))
            if not matches:
                raise ValueError("간접 식별 위치가 없습니다")
            spans.extend(matches)
    return spans


def _masked(text: str, spans: list[tuple[int, int]], level: int) -> list[dict]:
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    result = []
    cursor = 0
    for start, end in merged:
        if start > cursor:
            result.append({"text": text[cursor:start]})
        result.extend(_redacted(level))
        cursor = end
    if cursor < len(text):
        result.append({"text": text[cursor:]})
    return result


def segment_view(mode: AccessMode, segment: dict) -> list[dict]:
    """발언자와 본문을 포함해 한 조각을 안전한 구간 목록으로 변환합니다."""
    if mode == "deny":
        raise ValueError("열람할 수 없는 문서")
    text = segment["text"]
    speaker = segment.get("speaker_label")
    if not isinstance(text, str) or (speaker is not None and not isinstance(speaker, str)):
        raise ValueError("유효하지 않은 문서 조각")
    if mode == "subject" and segment.get("article") not in SUBJECT_ARTICLES:
        return _redacted(4)
    if mode in {"full", "subject"}:
        return ([{"text": speaker}, {"text": ": "}] if speaker else []) + [{"text": text}]
    if mode != "masked" or segment.get("level") not in {"L1", "L2", "L3", "L4"}:
        raise ValueError("유효하지 않은 열람 모드 또는 등급")
    level = max(3, int(segment["level"][1]))
    if segment.get("sen"):
        return _redacted(level)
    if is_subject_department(segment):
        return _redacted(level)
    result = []
    if speaker:
        result.extend(_masked(speaker, _spans(segment, "speaker_label", speaker), level))
        result.append({"text": ": "})
    result.extend(_masked(text, _spans(segment, "text", text), level))
    return result
