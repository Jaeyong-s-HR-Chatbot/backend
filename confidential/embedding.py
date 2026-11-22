"""가린 조각 전용 로컬 임베딩."""
import logging
import math
from functools import lru_cache

from app import config

log = logging.getLogger(__name__)
DIMENSIONS = 1024


@lru_cache(maxsize=1)
def model():
    try:
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer(config.EMBED_MODEL)
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        log.warning("기밀문서 임베딩 모델을 사용할 수 없어 키워드 검색을 사용합니다: %s", type(exc).__name__)
        return None


def encode(texts):
    embedder = model()
    if embedder is None:
        return None
    vectors = embedder.encode(texts, normalize_embeddings=True)
    if len(vectors) != len(texts):
        raise ValueError("임베딩 결과 개수가 맞지 않습니다")
    result = []
    for vector in vectors:
        values = [float(value) for value in vector]
        if (len(values) != DIMENSIONS or not all(math.isfinite(value) for value in values)
                or not math.isclose(sum(value * value for value in values), 1.0, rel_tol=1e-3, abs_tol=1e-3)):
            raise ValueError("임베딩 차원 또는 정규화가 올바르지 않습니다")
        result.append(values)
    return result


def literal(vector):
    return "[" + ",".join(format(value, ".9g") for value in vector) + "]"
