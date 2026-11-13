"""규정 검색. 출력 형식만 지키면 그래프는 그대로 쓸 수 있어요.

약속한 출력: [{id, citation, revised, text, score}]  (score 0~1, 높은 순)

검색 방식 (SEARCH_MODE)
- keyword : 질문 단어가 조문에 얼마나 들어 있는지 (흔한 단어는 낮게, 조문 제목에 있으면 2배). 추가 설치 없이 동작
- vector  : 질문을 BGE-M3로 벡터로 바꿔 pgvector 코사인 거리로 검색 (sentence-transformers 필요)
- hybrid  : 두 결과를 RRF로 합침
- auto    : 임베딩 모델을 쓸 수 있으면 hybrid, 아니면 keyword
- rag     : 옆 폴더의 rag 패키지(rag-main)가 검색 (Dense + BM25 → RRF → 선택 재정렬). 못 불러오면 auto
조문은 DB(rag.chunks) → 청크 파일(RULES_CHUNKS_PATH) → 내장 예시 5개 순서로 찾아 써요.
"""
import json
import logging
import math
import re
from functools import lru_cache
from pathlib import Path

from .. import config
from ..fake_data import SAMPLE_CHUNKS

log = logging.getLogger("hr_chat")
_STOP = {"며칠", "가능", "필요", "방법", "경우", "언제야", "어때", "얼마나", "얼마까지", "오늘", "지금", "얼마", "얼마야", "얼마예요", "있어", "있나요", "있어요", "몇", "개야", "어떻게", "받을", "돼", "되나요", "알려줘", "기준은",
         "뭐야", "뭐예요", "언제", "어디", "하면", "해야", "하나요", "인가요", "제가", "저는", "나는", "우리", "회사", "누리철도"}
_JOSA = re.compile(r"(으로|에서|에게|까지|부터|이랑|하고|은|는|이|가|을|를|의|에|도|만|로|와|과)$")


def _cite(doc_name, article, title):
    art = (article or "").strip()
    t = (title or "").strip()
    return f"{doc_name} {art}" + (f"({t[:20]})" if t and art.startswith("제") else "")


@lru_cache(maxsize=1)
def _chunks() -> list[dict]:
    """검색 대상 조문 전체 (약 1,100개라 메모리에 올려 둠)"""
    if config.USE_DB:
        from .. import db
        rows = db.fetch_all("SELECT chunk_id, doc_name, article, article_title, effective_date, text FROM rag.chunks "
                            "WHERE searchable ORDER BY chunk_id")
        return [{"id": r["chunk_id"], "citation": _cite(r["doc_name"], r["article"], r["article_title"]),
                 "revised": r["effective_date"].isoformat() if r["effective_date"] else "", "text": r["text"]} for r in rows]
    p = config.RULES_CHUNKS_PATH
    if p and Path(p).exists():
        out = []
        for line in open(p, encoding="utf-8"):
            r = json.loads(line)
            if not r.get("searchable"):
                continue
            if "chunk_id" in r:     # DB 저장소의 chunks.jsonl 형식
                out.append({"id": r["chunk_id"], "citation": _cite(r["doc_name"], r.get("article"), r.get("article_title")),
                            "revised": r.get("effective_date") or "", "text": r["text"]})
            else:                   # nuri_rules_chunks.jsonl 형식
                out.append({"id": r["id"], "citation": r["citation"], "revised": r.get("revised") or "", "text": r["text"]})
        return out
    return []


_END = re.compile(r"(하려면|하려고|하면|하고|해야|해요|했어|하나요|되나요|인가요|인데|이고|이야|예요|이에요|입니다|받아요|나요|어요|아요|해)$")


@lru_cache(maxsize=1)
def _corpus() -> str:
    return "\n".join(c["citation"] + " " + c["text"] for c in _chunks())


def _stem(w: str) -> str:
    """조사·어미를 뗌. 규정에 그대로 나오는 단어(연차휴가, 병가)는 건드리지 않고, 떼고 나서 한 글자만 남아도 그대로 둠"""
    if w in _corpus():
        return w
    for pat in (_END, _JOSA):
        x = pat.sub("", w)
        if len(x) >= 2:
            w = x
    return w


_EVENT_TERMS = {"결혼": "청원휴가 경조", "출산": "출산휴가 배우자 출산", "사망": "청원휴가 조위금", "돌아가": "청원휴가 조위금",
                "부고": "청원휴가 조위금", "이사": "이전비", "퇴직": "퇴직금"}


@lru_cache(maxsize=1)
def _glossary() -> list[tuple[list[str], str]]:
    """직원 말투 → 규정 용어 (app/data/glossary.csv). 예: 월급날 → 보수지급일"""
    import csv
    p = Path(__file__).resolve().parent.parent / "data" / "glossary.csv"
    if not p.exists():
        return []
    out = []
    for r in csv.DictReader(open(p, encoding="utf-8-sig")):
        words = [r["term"]] + [x for x in (r.get("synonyms") or "").split("|") if x]
        out.append((sorted({w.strip() for w in words if w.strip()}, key=len, reverse=True), r["standard_term"].strip()))
    return out


def rewrite(query: str) -> str:
    """질의 재작성: 질문에 직원 말투가 있으면 규정 용어를 덧붙임"""
    add = [std for words, std in _glossary() if std and std not in query and any(w in query for w in words)]
    add += [t for k, t in _EVENT_TERMS.items() if k in query]
    return query + (" " + " ".join(dict.fromkeys(add)) if add else "")


def _tokens(q: str) -> list[str]:
    q = re.sub(r"\[[^\]]*\]", " ", q)                      # [전화] 같은 마스킹 표시는 검색어에서 뺌
    words = [_stem(w) for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", q)]
    return list(dict.fromkeys(w for w in words if len(w) >= 2 and w not in _STOP))


def _keyword(query: str, k: int) -> list[dict]:
    toks, chunks = _tokens(query), _chunks()
    if not toks or not chunks:
        return []
    idf = {t: math.log((len(chunks) + 1) / (1 + sum(t in c["text"] or t in c["citation"] for c in chunks))) + 0.1 for t in toks}
    total = sum(idf.values())
    hits = []
    for c in chunks:
        got = sum(idf[t] * (2 if t in c["citation"] else 1) for t in toks if t in c["text"] or t in c["citation"])
        if got:
            hits.append(({**c, "score": round(min(1.0, got / total), 3)}, sum(c["text"].count(t) for t in toks)))
    hits.sort(key=lambda h: (h[0]["score"], h[1]), reverse=True)
    return [h[0] for h in hits[:k]]


@lru_cache(maxsize=1)
def _embedder():
    """BGE-M3. 설치돼 있지 않거나 모델을 못 받으면 None → keyword로 동작"""
    try:
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer(config.EMBED_MODEL)
    except Exception as e:
        log.warning("임베딩 모델을 쓸 수 없어 keyword 검색으로 동작해요: %s", type(e).__name__)
        return None


def vector_search(vec: list[float], k: int) -> list[dict]:
    """질문 벡터로 pgvector 검색 (코사인 유사도 = 1 - 거리)"""
    from .. import db
    lit = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
    rows = db.fetch_all("SELECT chunk_id, doc_name, article, article_title, effective_date, text, 1 - (embedding <=> %s::vector) AS score "
                        "FROM rag.chunks WHERE searchable AND embedding IS NOT NULL ORDER BY embedding <=> %s::vector LIMIT %s", (lit, lit, k))
    return [{"id": r["chunk_id"], "citation": _cite(r["doc_name"], r["article"], r["article_title"]),
             "revised": r["effective_date"].isoformat() if r["effective_date"] else "", "text": r["text"],
             "score": round(max(0.0, min(1.0, float(r["score"]))), 3)} for r in rows]


def _vector(query: str, k: int) -> list[dict]:
    m = _embedder()
    if m is None or not config.USE_DB:
        return []
    return vector_search(m.encode([re.sub(r"\[[^\]]*\]", " ", query)], normalize_embeddings=True)[0].tolist(), k)


def _rrf(lists: list[list[dict]], k: int, c: int = 60) -> list[dict]:
    """여러 검색 결과를 순위로 합침. 점수는 각 방식의 점수 중 높은 값을 남김"""
    rank, best = {}, {}
    for lst in lists:
        for i, h in enumerate(lst):
            rank[h["id"]] = rank.get(h["id"], 0) + 1 / (c + i + 1)
            if h["id"] not in best or h["score"] > best[h["id"]]["score"]:
                best[h["id"]] = h
    return [best[i] for i in sorted(rank, key=rank.get, reverse=True)[:k]]


@lru_cache(maxsize=1)
def _rag():
    """rag 패키지의 검색 함수 (SEARCH_MODE=rag). 못 불러오면 None → 기본 검색으로 동작"""
    import sys
    root = Path(config.RAG_PATH) if config.RAG_PATH else Path(__file__).resolve().parents[3] / "rag-main"
    if (root / "rag" / "__init__.py").exists() and str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from rag.backend import get_pipeline, search as rag_search
        info = get_pipeline().info()          # 조각 읽기 + 색인 (처음 한 번, 20초쯤)
        log.info("rag 패키지로 검색해요: %s", info)
        return rag_search
    except Exception as e:
        log.warning("rag 패키지를 쓸 수 없어 기본 검색으로 동작해요: %s: %s", type(e).__name__, str(e)[:120])
        return None


def mode() -> str:
    m = config.SEARCH_MODE
    if m == "rag":
        if _rag() is not None:
            return "rag"
        m = "auto"
    if m == "auto":
        return "hybrid" if (config.USE_DB and _embedder() is not None) else "keyword"
    return m


def search(query: str, k: int = 5, wide: bool = False) -> list[dict]:
    if config.SEARCH_MODE == "rag" and _rag() is not None:      # 질의 재작성·검색·(선택)재정렬을 rag 패키지가 함
        return _rag()(query, k, wide)
    k = k * 2 if wide else k
    # 내장 예시: DB도 청크 파일도 없을 때만 사용
    if not _chunks():
        hits = [{**{x: c[x] for x in ("id", "citation", "revised", "text", "canned", "rates") if x in c}, "score": 1.0}
                for c in SAMPLE_CHUNKS if any(kw in query for kw in c["keywords"])]
        return hits[:k]
    query = rewrite(query)
    m = mode()
    if m == "rag":
        m = "auto"
    if m == "keyword":
        return _keyword(query, k)
    if m == "vector":
        return _vector(query, k) or _keyword(query, k)
    return _rrf([_vector(query, 20), _keyword(query, 20)], k)
