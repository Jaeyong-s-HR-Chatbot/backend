"""API 요청·응답 형식. 프론트 src/types.ts 와 이름을 맞춰요.

프론트가 지금 읽는 필드: answer, citations[].title, refused
나머지(kind, thread_id, citations[].chunk_id/revised)는 추가 필드라 지금 프론트는 무시하고, 나중에 화면에서 쓸 수 있어요.
"""
from typing import Literal

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(description="사번")
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class Me(BaseModel):
    employee_id: str
    name: str
    dept: str
    grade: str
    role: Literal["employee", "hr_admin"]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    thread_id: str | None = Field(default=None, description="(추가) 대화 이어가기용. 지금 프론트는 안 보냄")


class Citation(BaseModel):
    title: str
    chunk_id: str | None = None
    revised: str | None = None


class ChatResponse(BaseModel):
    answer: str
    citations: list[Citation] = []
    refused: bool = False
    kind: Literal["answer", "refused", "sensitive"] = "answer"
    thread_id: str | None = None
    pii_notice: str | None = None
