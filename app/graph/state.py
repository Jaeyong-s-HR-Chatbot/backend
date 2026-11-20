"""파이프라인 단계 사이에 들고 다니는 값 (HRState). 팀 합의 대상이에요."""
from typing import Literal, TypedDict

Intent = Literal["rule", "lookup", "both", "sensitive", "refuse", "out_of_scope",
                 "data"]   # data는 인사팀(hr_admin) 전용


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
    question: str              # 원문 질문. 분류·검색·조회·답변은 이것으로 (로그에는 저장 안 함)
    # guard
    masked_question: str       # 개인정보를 가린 사본. 로그 저장용 + 규칙 분류의 타인 사번 태그 탐지용
    pii_found: dict[str, int]  # 종류별 마스킹 횟수
    has_other_emp_id: bool     # 질문에서 타인 사번을 감지했는지
    # classify
    intent: Intent
    conf_doc: str              # 보안문서가 필요한 질문: labor_minutes(노사회의록) / discipline_minutes(징계회의록)
    conf_doc_role: str         # 그 문서를 볼 수 있는 역할 (HR_LABOR / HR_DISCIPLINE)
    sensitive_topic: bool     # 인사팀이 민감 주제를 물음 → 검색은 하되 로그는 [민감 주제 문의]로
    admin_tool: str            # 인사팀 집계 도구 이름 (app/admin_tools.py)
    admin_params: dict         # 그 도구의 인자 (질문에서 뽑은 기간·기준값)
    search_hint: str           # 규정 검색어에 덧붙일 말 (인사팀 근태·촉진 도구가 근거 조문을 찾도록)
    admin_result: dict         # 집계 결과 {"기준", "건수", "표"}
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
    kind: Literal["answer", "refused", "sensitive"]   # out_of_scope는 refused로 안내
