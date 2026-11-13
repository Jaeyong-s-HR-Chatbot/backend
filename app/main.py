"""누리철도 HR 챗봇 API (FastAPI)

프론트(frontend-main)가 기대하는 3개:
  POST /auth/login  {username, password} → {access_token}
  GET  /me          (토큰)               → {employee_id, name, dept, grade, role}
  POST /chat        {message} + 토큰     → {answer, citations:[{title}], refused}
실행: uvicorn app.main:app --reload --port 8000
"""
import logging
import uuid

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from . import config
from .auth import authenticate, create_token, current_user
from . import db
from .graph import retriever
from .graph.build import run_pipeline
from .schemas import ChatRequest, ChatResponse, Citation, LoginRequest, LoginResponse, Me

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

app = FastAPI(title="누리철도 HR 챗봇 API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.get("/health")
def health():
    return {"ok": True, "llm": "fake" if config.USE_FAKE_LLM else config.LLM_MODEL,
            "db": db.ping() if config.USE_DB else "fake (HRDB_URL 없음)",
            "search": retriever.mode() if (config.USE_DB or config.RULES_CHUNKS_PATH) else "내장 예시 5개",
            "pseudo_key": config.PSEUDO_KEY_STATUS}


@app.post("/auth/login", response_model=LoginResponse)
def login(body: LoginRequest):
    user = authenticate(body.username, body.password)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "사번 또는 비밀번호가 맞지 않아요.")
    return LoginResponse(access_token=create_token(user["employee_id"], user["role"]))


@app.get("/me", response_model=Me)
def me(user: Me = Depends(current_user)):
    return user


@app.post("/chat", response_model=ChatResponse)
def chat(body: ChatRequest, user: Me = Depends(current_user)):
    # 사번은 토큰에서만 가져와요. 질문 글 속 사번은 쓰지 않아요.
    state = run_pipeline(user.employee_id, user.role, body.message, own_name=user.name)
    kind = state.get("kind", "answer")
    citations = [Citation(title=c["citation"], chunk_id=c.get("id"), revised=c.get("revised") or None)
                 for c in state.get("chunks", [])[:3]] if kind == "answer" else []
    pii_found = state.get("pii_found", {})
    pii_notice = (
        f"입력하신 개인정보({', '.join(pii_found)})는 가려서 처리했어요."
        if pii_found else None
    )
    return ChatResponse(
        answer=state.get("answer", ""),
        citations=citations,
        refused=(kind == "refused"),
        kind=kind,
        thread_id=body.thread_id or uuid.uuid4().hex,
        pii_notice=pii_notice,
    )
