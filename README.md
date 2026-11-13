# 누리철도 HR 챗봇 백엔드 (FastAPI + 파이프라인)

프론트(`frontend-main`)가 기대하는 API 형식 그대로 응답하는 뼈대예요.
흐름은 팀 설계도와 같고, DB와 LLM 주소가 있으면 실제로 연결해요. 프레임워크(LangGraph) 없이 함수 파이프라인으로 구현했어요.

## 실행

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # Windows: copy .env.example .env
uvicorn app.main:app --reload --port 8000
```

- API 문서: http://localhost:8000/docs
- 상태 확인: http://localhost:8000/health
- 테스트: `python -m pytest -q`

## 프론트 연결

프론트 폴더의 `.env`

```
VITE_API_URL=http://localhost:8000
VITE_USE_MOCK=false
```

테스트 계정 (DB 없이 돌릴 때. DB를 연결하면 `RUN_LOCAL.md`의 계정을 써요)

| 사번 | 이름 | 역할 |
| --- | --- | --- |
| E1584 | 이서준 | employee |
| E1003 | 송정식 | hr_admin |

## API

| 요청 | 보내는 값 | 받는 값 |
| --- | --- | --- |
| `POST /auth/login` | `{ username, password }` | `{ access_token }` |
| `GET /me` | 토큰 | `{ employee_id, name, dept, grade, role }` |
| `POST /chat` | `{ message, thread_id? }` + 토큰 | `{ answer, citations: [{ title, chunk_id, revised }], refused, kind, thread_id, pii_notice }` |

- 프론트가 지금 읽는 건 `answer`, `citations[].title`, `refused`만이에요. 나머지는 추가 필드라 지금 화면에는 영향이 없어요.
- `pii_notice`는 질문에서 마스킹한 개인정보 종류를 알려 줘요. 예: `입력하신 개인정보(전화)는 가려서 처리했어요.`
- `kind`: `answer` / `refused`(근거 없음·타인 조회) / `sensitive`(고충처리 안내)
- 사번은 토큰에서만 꺼내요. 질문 글 속 사번은 쓰지 않아요.

## 처리 흐름 (`app/graph/build.py`)

```
guard → classify ─┬─ rule ─────────────→ retrieve → calc → generate → verify ─┬─ 통과 → log
                  ├─ lookup → lookup ──────────────────→ generate             ├─ 근거 부족 → retrieve (1회)
                  ├─ both/event → lookup → retrieve                            └─ 그래도 실패 → refuse → log
                  ├─ sensitive → sensitive → log
                  └─ refuse(타인 조회) → refuse → log
```

## DB·LLM 연결 상태

`.env`에 주소가 있으면 실제로 연결하고, 비어 있으면 가짜 값으로 동작해요. 실행 방법은 `RUN_LOCAL.md`를 보세요.

| 부분 | 주소가 있을 때 | 비어 있을 때 |
| --- | --- | --- |
| 로그인 (`auth.py`) | `hr.users` 계정과 비밀번호 해시 확인 | 가짜 계정 2개 |
| 본인 조회 (`tools.py`) | `hr` 스키마에서 정해진 SQL로 조회 (연차, 복지포인트, 급여, 가족, 신청, 교육) | 가짜 값 |
| 규정 검색 (`graph/retriever.py`) | `rag.chunks`에서 키워드 검색, 임베딩 모델이 있으면 벡터와 합침. `SEARCH_MODE=rag`면 `rag-main` 패키지 사용 | 내장 예시 5개 |
| 답변 생성 (`graph/nodes.py`) | Ollama·vLLM 호출 | 가짜 LLM (조회 값과 가장 가까운 조문 표시) |
| 대화 로그 (`app.chat_log`) | `CHAT_LOG_DB=true`일 때 마스킹 질문·가명 ID·안전한 메타데이터 저장 | 안전한 항목만 콘솔 로그 |

## 아직 안 된 부분

| 항목 | 지금 | 할 일 | 담당 |
| --- | --- | --- | --- |
| 마스킹 (`guard`) | 질문·로그 답변의 개인정보를 가리고, 민감 주제 질문은 로그에 `[민감 주제 문의]`로 저장. 자세한 기준은 [마스킹 안내](./docs/masking.md) 참고 | 이름·주소·병명 등 비정형 개인정보는 가리지 않음 | 진태현 |
| 계산 (`calc`) | 예시 출장비만 | 기준표(`rules.json`) 기반 계산 함수 | 진태현 |
| 검색 고도화 | 기본은 키워드 + (선택) 벡터. `SEARCH_MODE=rag`면 옆 폴더 `rag-main` 패키지(Dense + BM25 → RRF, 선택 재정렬)로 검색 | 재정렬 채택 확정, 임계값 보정. BM25만 켠 rag 검색은 "근거 없음" 거절이 약함 | 이재용 · 진태현 |
| 권한 | 코드에서 사번을 토큰으로 고정, 개인정보 테이블은 읽지 않음 | 챗봇 전용 계정(`chatbot_ro`), RLS | 박규태 |
| 로그 | HMAC-SHA256 가명 ID와 마스킹 질문·답변, 분류·근거·도구명·처리시간 저장. 조회 도구를 쓴 답변은 `answer_masked`를 비워 둠. [마스킹 안내](./docs/masking.md) 참고 | `.env`에 고정 `PSEUDO_KEY`를 안전하게 설정해야 재시작 후에도 가명 ID가 유지됨 | 박규태 · 진태현 |
| 인사팀 전용 조회 | 없음 (일반 직원과 같음) | 신청 현황·부서 통계 도구 | 진태현 |
| 생애 이벤트 계획, 신청서 초안 | 없음 | 이후 | |

검색기는 `search(query, k) -> [{id, citation, revised, text, score}]` 형식만 지키면 파이프라인을 안 고치고 바꿔 끼울 수 있어요.
