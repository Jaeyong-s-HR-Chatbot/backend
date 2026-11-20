"""설정값. 모두 .env(또는 환경변수)에서 읽어요. 코드에 주소·키를 직접 쓰지 않아요."""
import os
import logging
import secrets
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


log = logging.getLogger(__name__)

# 로그인 토큰
JWT_SECRET = _get("JWT_SECRET", "dev-only-change-me-please-32bytes-min!!")
JWT_EXPIRE_MIN = int(_get("JWT_EXPIRE_MIN", "480"))
PSEUDO_KEY = _get("PSEUDO_KEY")
PSEUDO_KEY_STATUS = "fixed" if PSEUDO_KEY else "temporary"
if not PSEUDO_KEY:
    PSEUDO_KEY = secrets.token_hex(32)
    log.warning("PSEUDO_KEY가 없어 임시 키를 써요. 서버를 다시 켜면 가명 ID가 바뀌어요")
CHAT_LOG_DB = _get("CHAT_LOG_DB", "true").lower() not in {"0", "false", "no", "off"}
SENSITIVE_LOG_PSEUDO = _get("SENSITIVE_LOG_PSEUDO", "keep").lower()
if SENSITIVE_LOG_PSEUDO not in {"keep", "drop"}:
    log.warning("SENSITIVE_LOG_PSEUDO 값이 올바르지 않아 keep 방식으로 동작합니다")
    SENSITIVE_LOG_PSEUDO = "keep"

# 프론트 주소 (CORS). 쉼표로 여러 개
CORS_ORIGINS = [o.strip() for o in _get("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()]

# LLM (OpenAI 형식 서버, 예: Colab vLLM). 비어 있으면 가짜 LLM으로 동작
LLM_BASE_URL = _get("LLM_BASE_URL")
LLM_MODEL = _get("LLM_MODEL", "qwen3.5-9b")
LLM_API_KEY = _get("LLM_API_KEY", "EMPTY")

# 규정 청크 파일. 있으면 가짜 검색이 이 파일에서 키워드로 찾아요. 없으면 내장 예시 5개만 사용
RULES_CHUNKS_PATH = _get("RULES_CHUNKS_PATH")

USE_FAKE_LLM = not LLM_BASE_URL
LLM_TIMEOUT = float(_get("LLM_TIMEOUT", "180"))   # 노트북에서 조문 5개를 읽으면 오래 걸릴 수 있어 넉넉하게
LLM_MAX_TOKENS = int(_get("LLM_MAX_TOKENS", "600"))

# 인사·규정 DB (PostgreSQL + pgvector). 비어 있으면 DB 없이 가짜 데이터로 동작
#   예: postgresql://postgres:비밀번호@localhost:5432/hrdb
HRDB_URL = _get("HRDB_URL")
USE_DB = bool(HRDB_URL)
if not USE_DB:
    CHAT_LOG_DB = False

# 규정 검색 방식: auto(임베딩 모델이 설치돼 있으면 hybrid, 없으면 keyword) / keyword / vector / hybrid / rag
#   rag: 옆 폴더의 rag 패키지(rag-main)로 검색. 못 불러오면 auto로 동작
SEARCH_MODE = _get("SEARCH_MODE", "auto")
RAG_PATH = _get("RAG_PATH")          # rag 패키지 폴더. 비우면 backend 옆의 rag-main
EMBED_MODEL = _get("EMBED_MODEL", "BAAI/bge-m3")

# 근거 검증: 검색 1위 점수가 이보다 낮으면 다시 검색하고, 그래도 낮으면 거절
VERIFY_MIN_SCORE = float(_get("VERIFY_MIN_SCORE", "0.3"))

# 계산 도구와 rules.json
CALC_TOOLS = _get("CALC_TOOLS", "off").lower() in {"1", "true", "yes", "on"}
RULES_PATH = _get(
    "RULES_PATH",
    str(Path(__file__).resolve().parents[2] / "DB" / "rules" / "rules.json"),
)
