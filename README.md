# 누리철도 HR 챗봇 백엔드 (FastAPI + 파이프라인)

프론트(`frontend`)가 기대하는 API 형식 그대로 응답하는 뼈대예요.
흐름은 팀 설계도와 같고, DB와 LLM 주소가 있으면 실제로 연결해요. 프레임워크(LangGraph) 없이 함수 파이프라인으로 구현했어요.

## 실행

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # Windows: copy .env.example .env
uvicorn app.main:app --port 8000
```

- API 문서: http://localhost:8000/docs
- 상태 확인: http://localhost:8000/health
- 윈도우에서는 `run_backend.bat`을 더블클릭해도 서버가 켜져요.
- 코드를 고친 뒤에는 서버를 껐다 켜야 반영돼요.
- 테스트: `python -m pytest -q`
  - DB가 연결된 환경에서는 `tests/test_rag_mode.py` 1건이 실패해요. 테스트 기대값 문제이고 동작은 정상이에요.
- 마스킹 측정: `python scripts/eval_masking.py` (결과는 [측정 결과](./docs/masking_eval.md))

DB와 로컬 LLM(Ollama)까지 연결해 실행하는 방법은 `RUN_LOCAL.md`를 보세요.

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

| 단계 | 하는 일 |
| --- | --- |
| `guard` | 질문의 개인정보를 태그로 가려요. 이후 단계와 로그는 가려진 질문만 써요 |
| `classify` | 키워드 규칙으로 의도를 나눠요 (`rule` / `lookup` / `both` / `event` / `sensitive` / `refuse`) |
| `lookup` | 본인 데이터를 정해진 SQL로 조회해요 |
| `retrieve` | 규정 조각을 검색해요 |
| `calc` | 근거에 단가가 있으면 금액을 계산해요 (지금은 출장비만) |
| `generate` | LLM을 호출하는 유일한 단계예요 |
| `verify` | 근거 점수가 기준에 못 미치면 한 번 다시 검색하고, 그래도 안 되면 거절해요 |
| `log` | 가명 ID와 마스킹된 내용만 저장해요 |

## DB·LLM 연결 상태

`.env`에 주소가 있으면 실제로 연결하고, 비어 있으면 가짜 값으로 동작해요. 실행 방법은 `RUN_LOCAL.md`를 보세요.

| 부분 | 주소가 있을 때 | 비어 있을 때 |
| --- | --- | --- |
| 로그인 (`auth.py`) | `hr.users` 계정과 비밀번호 해시 확인 | 가짜 계정 2개 |
| 본인 조회 (`tools.py`) | `hr` 스키마에서 정해진 SQL로 조회 (연차, 복지포인트, 급여, 가족, 신청, 교육) | 가짜 값 |
| 규정 검색 (`graph/retriever.py`) | `rag.chunks`에서 키워드 검색, 임베딩 모델이 있으면 벡터와 합침. `SEARCH_MODE=rag`면 `RAG-taehyeon` 패키지 사용 (`RAG_PATH`로 위치 지정) | 내장 예시 5개 |
| 답변 생성 (`graph/nodes.py`) | Ollama·vLLM 호출. 답변 길이는 `LLM_MAX_TOKENS`(기본 600)로 제한 | 가짜 LLM (조회 값과 가장 가까운 조문 표시) |
| 대화 로그 (`app.chat_log`) | `CHAT_LOG_DB=true`일 때 마스킹 질문·가명 ID·안전한 메타데이터 저장 | 안전한 항목만 콘솔 로그 |

## 마스킹

질문이 LLM과 로그에 들어가기 전에 개인정보를 태그로 바꿔요. 원래 값은 어디에도 저장하지 않고, 되돌리는 표도 만들지 않아요.

| 태그 | 대상 |
| --- | --- |
| `[주민번호]` `[카드]` `[전화]` `[이메일]` `[계좌]` | 번호 형식 개인정보 |
| `[본인사번]` `[본인이름]` | 로그인한 본인의 사번과 이름 |
| `[사번]` | 다른 직원의 사번 (이 경우 조회는 거절해요) |

- 로그의 사용자 식별자는 사번이 아니라 가명 ID(`P-` + HMAC-SHA256 앞 12자리)예요.
- 본인 조회 답변(연차, 급여 등)은 로그에 저장하지 않아요 (`answer_masked`가 비어 있어요).
- 민감 주제 질문은 본문 대신 `[민감 주제 문의]`로 남겨요.

측정 결과 (직접 만든 가짜 사례 기준, `tests/masking_cases.py`)

| 항목 | 보강 전 | 보강 후 |
| --- | --- | --- |
| 가려야 하는 태그를 맞게 가림 | 88 / 99 | 99 / 99 |
| 가리면 안 되는 문장을 가림 | 2 / 54 | 0 / 54 |

사례에 없는 형태는 놓칠 수 있어요. 기준은 [마스킹 안내](./docs/masking.md), 사례별 결과는 [측정 결과](./docs/masking_eval.md)를 보세요.

## 아직 안 된 부분

| 항목 | 지금 | 할 일 | 담당 |
| --- | --- | --- | --- |
| 마스킹 (`guard`) | 태그 8종으로 가리고 측정 사례로 확인. 자세한 내용은 위 "마스킹" 절 참고 | 타인 이름·주소·병명은 가리지 않음 (팀 결정 필요). 글자로 풀어 쓴 번호는 못 잡음 | 진태현 |
| 의도 분류 (`classify`) | 키워드 규칙. 개인정보를 소개하는 문장("연락처는 …이에요")은 본인 조회 판단에서 빼고, 신청 방법 질문과 신청 현황 조회를 구분. 민감 주제·타인 조회는 문장 전체로 검사 | 규칙에 없는 표현은 잘못 분류될 수 있음. 애매한 질문만 LLM으로 보완할지 결정 | 진태현 |
| 계산 (`calc`) | 예시 출장비만. 연차 일수 같은 계산은 LLM이 직접 해서 느리고 틀릴 수 있음 | 기준표(`rules.json`) 기반 계산 함수 | 진태현 |
| 검증 (`verify`) | 검색 점수가 기준(0.3) 이상이면 통과 | 키워드만 겹쳐도 통과해 근거 없이 답하는 경우가 있음. 재정렬 점수 기반으로 교체, 답변 숫자와 근거 대조 | 진태현 |
| 검색 고도화 | 기본은 키워드 + (선택) 벡터. `SEARCH_MODE=rag`면 옆 폴더 `RAG-taehyeon` 패키지(Dense + BM25 → RRF, 선택 재정렬)로 검색 | 재정렬 채택 확정, 임계값 보정. BM25만 켠 rag 검색은 "근거 없음" 거절이 약함 | 이재용 · 진태현 |
| 권한 | 코드에서 사번을 토큰으로 고정, 개인정보 테이블은 읽지 않음 | 챗봇 전용 계정(`chatbot_ro`), RLS | 박규태 |
| 로그 | HMAC-SHA256 가명 ID와 마스킹 질문·답변, 분류·근거·도구명·처리시간 저장. 조회 도구를 쓴 답변은 `answer_masked`를 비워 둠. [마스킹 안내](./docs/masking.md) 참고 | `.env`에 고정 `PSEUDO_KEY`를 안전하게 설정해야 재시작 후에도 가명 ID가 유지됨. "근거 없음"과 "권한 없음"을 구분하는 항목 추가 | 박규태 · 진태현 |
| 인사팀 전용 조회 | 없음 (일반 직원과 같음) | 신청 현황·부서 통계 도구 | 진태현 |
| 생애 이벤트 계획, 신청서 초안 | 없음 | 이후 | |

검색기는 `search(query, k) -> [{id, citation, revised, text, score}]` 형식만 지키면 파이프라인을 안 고치고 바꿔 끼울 수 있어요.
