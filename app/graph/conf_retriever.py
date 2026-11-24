"""권한 범위 안의 가린 사본으로 기밀문서 조각을 검색합니다."""
import re
from collections import defaultdict

from .. import config, db, roles
from ..conf_access import SUBJECT_ARTICLES, access, segment_view
from ..conf_api import SAFE_HEADINGS
from confidential import embedding
from confidential.mask_rules import is_subject_department
from . import conf_doc

CATEGORIES = {conf_doc.DISCIPLINE_MINUTES: "징계", conf_doc.LABOR_MINUTES: "노사"}
STOP = {"위원회", "협의회", "회의", "내용", "알려줘", "뭐였어", "뭐였고", "뭐라고",
        "있어", "결과", "결과가", "승진", "언제", "가능해"}


def _tokens(question):
    words = re.findall(r"[가-힣A-Za-z0-9]{2,}", question.lower())
    words = [re.sub(r"(에서|으로|부터|까지|은|는|이|가|을|를|의|에)$", "", word) for word in words]
    if "결과" in question or "의결" in question:
        words.extend(["의결", "징계내용"])
    return list(dict.fromkeys(word for word in words if len(word) >= 2 and word not in STOP))


def _period_match(question, doc):
    quarter = re.search(r"([1-4])\s*분기", question)
    meeting = re.search(r"([1-9]\d*)\s*차", question)
    if quarter and doc["category"] == "노사":
        return f"q{quarter.group(1)}" in doc["doc_id"].lower()
    if meeting and doc["category"] == "징계":
        return bool(re.search(rf"(?:^|-)0*{meeting.group(1)}(?:-|$)", doc["doc_id"]))
    return True


def _rank_keyword(rows, question):
    tokens = _tokens(question)
    ranked = []
    for row in rows:
        haystack = " ".join((row["masked_text"], row["article"] or "", row["title"])).lower()
        matched = sum(token in haystack for token in tokens)
        if matched:
            ranked.append({**row, "score": matched / max(len(tokens), 1)})
    return sorted(ranked, key=lambda row: (-row["score"], row["chunk_id"]))


def search(emp_id, conf_type, question):
    if conf_type not in CATEGORIES:
        raise ValueError("지원하지 않는 기밀문서 분류")
    access_roles = roles.get_roles(emp_id)
    if not config.USE_DB:
        raise RuntimeError("기밀문서 DB가 설정되지 않았습니다")
    with db.connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        docs = conn.execute(
            """SELECT doc_id, title, confidential, category, doc_kind, level, allowed_roles,
                      subject_emp_id, participants
               FROM conf.documents WHERE category = %s AND status = 'ready'""",
            (CATEGORIES[conf_type],),
        ).fetchall()
        modes = {d["doc_id"]: access(emp_id, access_roles, d) for d in docs}
        authorized = {d["doc_id"]: d for d in docs if modes[d["doc_id"]] != "deny"}
        if not authorized:
            return {"authorized": False, "chunks": [], "documents": [], "hidden": []}
        permitted = {doc_id: doc for doc_id, doc in authorized.items() if _period_match(question, doc)}
        if not permitted:
            return {"authorized": True, "chunks": [], "documents": [], "hidden": []}
        full_ids = [doc_id for doc_id in permitted if modes[doc_id] == "full"]
        masked_ids = [doc_id for doc_id in permitted if modes[doc_id] == "masked"]
        subject_ids = [doc_id for doc_id in permitted if modes[doc_id] == "subject"]
        predicate = """(m.doc_id = ANY(%s) OR
                       (m.doc_id = ANY(%s) AND m.review_required = false) OR
                       (m.doc_id = ANY(%s) AND m.article = ANY(%s)))"""
        params = (full_ids, masked_ids, subject_ids, list(SUBJECT_ARTICLES))
        rows = conn.execute(
            f"""SELECT m.chunk_id, m.doc_id, m.article, m.masked_text, d.title,
                       m.embedding IS NOT NULL AS has_embedding
                FROM conf.chunks_masked m JOIN conf.documents d ON d.doc_id = m.doc_id
                WHERE {predicate}""", params,
        ).fetchall()
        keyword = _rank_keyword(rows, question)[:20]
        vector = []
        encoded = embedding.encode([question]) if any(row["has_embedding"] for row in rows) else None
        if encoded:
            value = embedding.literal(encoded[0])
            vector = conn.execute(
                f"""SELECT m.chunk_id, m.doc_id, m.article, m.masked_text, d.title,
                           1 - (m.embedding <=> %s::vector) AS score
                    FROM conf.chunks_masked m JOIN conf.documents d ON d.doc_id = m.doc_id
                    WHERE {predicate} AND m.embedding IS NOT NULL
                    ORDER BY m.embedding <=> %s::vector LIMIT 20""",
                (value, *params, value),
            ).fetchall()
        ranking = defaultdict(float)
        scores = defaultdict(float)
        for hits in (keyword, vector):
            for index, row in enumerate(hits):
                ranking[row["chunk_id"]] += 1 / (61 + index)
                scores[row["chunk_id"]] = max(scores[row["chunk_id"]], float(row["score"]))
        ids = sorted(ranking, key=lambda chunk_id: (-ranking[chunk_id], chunk_id))[:5]
        if not ids:
            return {"authorized": True, "chunks": [], "documents": [], "hidden": []}
        segments = conn.execute(
            f"""SELECT s.chunk_id, s.doc_id, s.chunk_type, s.article, s.speaker_label,
                       s.text, s.level, s.entities, s.og, s.pos, s.qi, s.sen, s.review_required
                FROM conf.segments s JOIN conf.chunks_masked m ON m.chunk_id = s.chunk_id
                WHERE {predicate} AND s.chunk_id = ANY(%s)""", (*params, ids),
        ).fetchall()
    by_id = {row["chunk_id"]: row for row in segments}
    if set(by_id) != set(ids):
        raise ValueError("검색 조각을 확인할 수 없습니다")
    chunks, references, hidden = [], {}, []
    for chunk_id in ids:
        row = by_id[chunk_id]
        doc, mode = permitted[row["doc_id"]], modes[row["doc_id"]]
        item = dict(row, level=row["level"] if row["review_required"] else doc["level"])
        rendered = segment_view(mode, item)
        text = "".join(part.get("text", "[가림]") for part in rendered)
        if mode != "full":
            hidden.extend((entity["text"], "[이름]" if entity.get("type") == "PS" else "[식별정보]")
                          for entity in row["entities"] if entity.get("text"))
            if row["sen"]:
                hidden.append((row["text"], "[가림]"))
            if mode == "masked" and is_subject_department(row):
                hidden.append((row["text"], "[부서]"))
            for pair in row["qi"]:
                if pair[3]:
                    hidden.extend(((pair[0], "[가림]"), (pair[1], "[가림]")))
        title = doc["title"] if mode == "full" else f"{doc['category']} 기밀문서"
        article = row["article"] or "본문"
        if mode != "full" and article not in SAFE_HEADINGS:
            article = "기타"
        chunks.append({"id": chunk_id, "doc_id": doc["doc_id"], "citation": f"{title} · {article}",
                       "text": text, "score": scores[chunk_id], "conf_mode": mode})
        references[doc["doc_id"]] = {"doc_id": doc["doc_id"], "title": title,
                                     "confidential": True, "view": "original" if mode == "full" else "redacted"}
    return {"authorized": True, "chunks": chunks, "documents": list(references.values()), "hidden": hidden}
