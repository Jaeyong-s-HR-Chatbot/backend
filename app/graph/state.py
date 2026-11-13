"""파이프라인 단계 사이에 들고 다니는 값 (HRState). 팀 합의 대상이에요."""
from typing import Literal, TypedDict

Intent = Literal["rule", "lookup", "both", "event", "sensitive", "refuse"]


class Chunk(TypedDict, total=False):
    id: str
    citation: str
    revised: str
    text: str
    score: float      # 검색·리랭커 점수 (0~1)
    canned: str       # 가짜 LLM 전용: 미리 넣은 답변


class HRState(TypedDict, total=False):
    # 입력 (서버가 채움)
    employee_id: str           # 토큰에서 꺼낸 사번. 질문 글에서 읽지 않음
    role: str                  # employee / hr_admin
    own_name: str              # /me에서 확인한 본인 이름 (답변 로그 마스킹용)
    question: str              # guard 입력 전용 원문 질문. guard 뒤 상태에서 제거
    # guard
    masked_question: str       # 개인정보를 가린 질문 (로그·LLM에는 이것만)
    pii_found: dict[str, int]  # 종류별 마스킹 횟수
    has_other_emp_id: bool     # 질문에서 타인 사번을 감지했는지
    # classify
    intent: Intent
    # lookup / retrieve / calc
    lookup: dict               # 본인 조회 결과
    tools_used: list[str]      # 사용한 조회 도구 이름 (값은 로그에 남기지 않음)
    chunks: list[Chunk]        # 검색된 근거 조문
    calc: dict                 # 계산 결과
    retries: int               # 재검색 횟수 (최대 1)
    # generate / verify
    answer: str
    answer_masked: str | None
    verified: bool
    latency_ms: int
    # 응답
    kind: Literal["answer", "refused", "sensitive"]
