# 누리철도 HR 챗봇 백엔드 (FastAPI + 파이프라인)

프론트(`frontend`)가 기대하는 API 형식 그대로 응답하는 백엔드예요.
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
- 테스트: `python -m pytest -q` (분류 보조 LLM은 테스트에서 기본으로 꺼져요)
  - DB가 연결된 환경에서는 `tests/test_rag_mode.py` 1건이 실패해요. 테스트 기대값 문제이고 동작은 정상이에요.
  - `tests/test_db.py`는 로컬 DB가 켜져 있어야 돌아가요.

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
| `GET /me` | 토큰 | `{ employee_id, name, dept, grade, role, conf_roles, clearance }` |
| `POST /chat` | `{ message, thread_id? }` + 토큰 | `{ answer, citations: [{ title, chunk_id, revised }], refused, kind, thread_id, pii_notice, documents }` |
| `GET /documents` | 토큰 | 볼 수 있는 기밀문서 목록 (징계 담당은 징계, 노무 담당은 노사, 관리자는 전체. 내용 없음) |
| `GET /documents/{doc_id}` | 토큰 | 역할별 원본 / 결과만 / 가림본 / 거부 (L4 원문 열람은 기록) |
| `POST /documents` · `DELETE /documents/{doc_id}` | PDF 파일 + 토큰 | 기밀문서 업로드·삭제 (담당 역할만) |

- 프론트가 채팅에서 읽는 건 `answer`, `citations[].title`, `refused`, `documents`(기밀문서 카드)예요.
- `pii_notice`는 질문을 마스킹하지 않게 바뀌어서 항상 `null`이에요 (화면 호환용으로 칸만 남김).
- `kind`: `answer` / `refused`(근거 없음·타인 조회·질문 없음·범위 밖) / `sensitive`(고충처리 안내)
- `conf_roles`: `EMPLOYEE`, `HR_DISCIPLINE`(징계 담당), `HR_LABOR`(노무 담당), `SYS_ADMIN`, `TEAM_LEAD` 등. `clearance`는 볼 수 있는 보안 등급
- 사번과 역할은 토큰에서만 꺼내요. 질문 글 속 사번이나 "나 인사팀인데" 같은 말은 권한에 쓰지 않아요.

## 처리 흐름 (`app/graph/build.py`)

자세한 기준·측정은 [의도 분류](./docs/intent_classification.md)를 보세요.

```
guard → classify ─┬─ sensitive (임직원) ────────→ 민감 안내
                  ├─ 대화 기록 거절 (인사팀) ───→ 거절 안내
                  ├─ 보안문서 질문 ─────────────→ conf_chat (권한 확인 → 권한 있는 문서만 검색 → 로컬 LLM 답변)
                  ├─ refuse (타인 조회) ────────→ 거절 안내
                  ├─ no_question ───────────────→ "어떤 내용이 궁금한지 알려 주세요"
                  ├─ out_of_scope ──────────────→ 범위 밖 안내
                  ├─ lookup ─→ 본인 조회 ───────────────────────→ generate → verify
                  ├─ both ───→ 본인 조회 / 인사팀 집계 → retrieve → calc → generate → verify ─┬─ 통과 → log
                  └─ rule ──────────────────────→ retrieve → calc → generate → verify ──────┤  근거 부족 → retrieve (1회)
                                                                                               └─ 그래도 실패 → 거절 (인사팀은 집계 표만)
```

| 단계 | 하는 일 |
| --- | --- |
| `guard` | 질문의 개인정보를 찾아 로그 저장용 사본만 태그로 가려요. 분류·검색·조회·답변은 원문 질문으로 해요 |
| `classify` | ① 보안문서(노사·징계 회의록) 필요 여부 → ② 의도 분류. 규칙이 먼저 하고 규칙이 약한 곳만 분류 보조 LLM에 물어요 (아래 "의도 분류") |
| `lookup` | 본인 데이터를 정해진 SQL로 조회해요 (사번은 토큰 것만, 질문 단어로 조회 종류를 골라요) |
| `admin_lookup` | 인사팀 집계 도구를 정해진 SQL로 실행해요 (`admin_tools.py`, hr_admin만) |
| `retrieve` | 규정 조각을 검색해요. 인사팀 집계 질문은 도구별 검색 힌트를 붙여요 |
| `calc` | 기준표로 금액·일수를 계산해요 (`CALC_TOOLS=on`일 때, 기본 off) |
| `generate` | 답변 LLM을 호출해요 (기밀문서 질문은 `conf_chat`이 로컬 LLM으로) |
| `verify` | 근거 점수가 기준(0.3)에 못 미치면 한 번 다시 검색하고, 그래도 안 되면 거절해요 |
| `log` | 가명 ID와 마스킹된 내용만 저장해요 |

## 의도 분류

| 의도 | 뜻 |
| --- | --- |
| `rule` | 규정·절차·조건만. 가정·일반 조건으로 값을 준 질문("통상임금 320만원이면") |
| `lookup` | 로그인한 본인의 저장 데이터만 |
| `both` | 본인 데이터 + 규정. 본인 상황을 말하며 값을 준 질문("제 통상임금 320만인데")도 both. 인사팀 집계 질문도 both |
| `sensitive` | 괴롭힘·성희롱 등 피해 호소 (임직원은 안내로 끝, 인사팀은 표시만 하고 계속) |
| `refuse` | 다른 직원 정보 요구 (임직원), 특정인 챗봇 대화·민감 상담자 신원 (인사팀) |
| `no_question` | 인사말·감사·자기소개만 |
| `out_of_scope` | 인사와 무관, 다른 조직의 제도 |

분류 보조 LLM (`CLASSIFY_LLM=on`, 실제 LLM이 있을 때만. 실패하면 규칙 결과)

| 자리 | 임직원 | 인사팀 |
| --- | --- | --- |
| ① 보안문서 판단 (`graph/conf_doc.py`) | 노사·징계·회의 단어가 있으면 로컬 LLM이 회의록 필요 여부 판단 | 같음 |
| ② 의도 (`graph/llm_classify.py`, `graph/admin_intent_llm.py`) | 규칙이 rule·out_of_scope면 LLM 의도, LLM이 sensitive·refuse면 그 의도, 계산 값 | 대화 기록 거절이 아니면 LLM이 의도·집계 도구를 고름 |
| ③ 기간·기준값 (`graph/admin_params_llm.py`) | – | 규칙이 비워 둔 값만, 질문에 근거가 있는 값만 |

LLM은 로컬에서 돌려요. 보안문서 판단·기밀문서 답변은 `LLM_BASE_URL`이 `localhost`·`127.0.0.1`일 때만 LLM을 써서, 같은 서버에 둬도 Docker 서비스 이름이나 사설 IP로 넣으면 LLM을 안 써요.

## 평가

| 무엇 | 명령 | 정답 데이터 |
| --- | --- | --- |
| 임직원 의도 | `python scripts/eval_intent.py --split test [--llm]` | `eval/intent/intent_cases.jsonl` (178) |
| 인사팀 의도·도구·인자 | `python scripts/eval_admin_intent.py [--llm]` | RAG 저장소 `eval/golden/admin_golden_set.json`을 `eval/golden/`에 복사 |
| 처음 보는 질문 | 위 두 스크립트에 `--cases eval/holdout/holdout_employee.jsonl --exclude H-E-016,H-E-096` / `--cases eval/holdout/holdout_admin.jsonl --exclude H-A-001` | `eval/holdout/` (GPT 생성, [README](./eval/holdout/README.md)) |
| 보안문서 판단 | `python scripts/eval_conf_doc.py [--llm]` | `eval/conf_doc/conf_doc_cases.jsonl` (41) |
| 검색·답변 (골든셋) | `python scripts/eval_golden.py retrieval --split test` 등 | RAG 저장소 `eval/golden/golden_set.json` |
| 마스킹 | `python scripts/eval_masking.py` | `tests/masking_cases.py` ([측정 결과](./docs/masking_eval.md)) |

- `--llm`은 `.env`의 LLM으로 규칙 vs 규칙+LLM을 비교하고, LLM 의도를 어디까지 쓸지 세 방식 비교표도 보여줘요.
- GPU가 없으면 Colab 노트북 [`notebooks/admin_intent_eval_27b.ipynb`](./notebooks/admin_intent_eval_27b.ipynb)로 Qwen3.5-27B를 띄워 세 자리와 처음 보는 질문을 한 번에 재요.
- 최근 결과(27B): 임직원 처음 보는 질문 92.2% · 기존 평가셋 97.9%, 인사팀 처음 보는 질문 의도·도구 100%(비교표) · 골든셋 99.2%, 보안문서 판단 재현율 96%.

## DB·LLM 연결 상태

`.env`에 주소가 있으면 실제로 연결하고, 비어 있으면 가짜 값으로 동작해요. 실행 방법은 `RUN_LOCAL.md`를 보세요.

| 부분 | 주소가 있을 때 | 비어 있을 때 |
| --- | --- | --- |
| 로그인 (`auth.py`) | `hr.users` 계정과 비밀번호 해시 확인, 역할은 `app.user_roles` | 가짜 계정 2개 |
| 본인 조회 (`tools.py`) | `hr` 스키마에서 정해진 SQL로 조회 (연차, 복지포인트, 급여, 가족, 신청, 교육) | 가짜 값 |
| 인사팀 집계 (`admin_tools.py`) | 집계 도구 49개를 정해진 SQL로 (개인정보 테이블은 안 읽음) | 사용 안 함 |
| 규정 검색 (`graph/retriever.py`) | `rag.chunks`(항 단위)에서 키워드 검색, 임베딩 모델이 있으면 벡터와 합침. `SEARCH_MODE=rag`면 예전 `RAG-taehyeon` 패키지 사용 (2026-11-19 보관 → [experiments/rag-taehyeon](https://github.com/Jaeyong-s-HR-Chatbot/experiments/tree/main/rag-taehyeon)) | 내장 예시 5개 |
| 기밀문서 (`conf_api.py`, `graph/conf_chat.py`) | `conf` 스키마에 원본·가린 사본 저장, 역할별 열람·검색 | 사용 안 함 |
| 분류 보조 LLM (`graph/llm_classify.py` 등) | Ollama `/api/chat`으로 JSON 스키마 응답 (`CLASSIFY_MODEL`, 비우면 `LLM_MODEL`) | 규칙만 |
| 답변 생성 (`graph/nodes.py`) | Ollama·vLLM 호출. 답변 길이는 `LLM_MAX_TOKENS`(기본 600)로 제한 | 가짜 LLM (조회 값과 가장 가까운 조문 표시) |
| 대화 로그 (`app.chat_log`) | `CHAT_LOG_DB=true`일 때 마스킹 질문·가명 ID·안전한 메타데이터 저장 | 안전한 항목만 콘솔 로그 |

로컬 CPU(qwen3.5:4b)로 분류 보조 LLM을 켜면 질문마다 15초 이상 늘어요. 시연할 때는 `CLASSIFY_LLM=off`나 GPU 서버를 쓰세요.

## 마스킹

질문은 마스킹하지 않고 원문 그대로 처리해요. 로그인과 ERP 연동이 되어 있어 본인이 개인정보를 직접 입력할 일이 드물기 때문이에요. 대신 **로그에 저장할 때** 개인정보를 태그로 바꿔요. 원래 값은 로그에 남기지 않고, 되돌리는 표도 만들지 않아요. 태그가 붙은 사본은 규칙 분류가 타인 사번(`[사번]`)과 개인정보 소개만 있는 문장을 알아보는 데에도 써요.

| 태그 | 대상 |
| --- | --- |
| `[주민번호]` `[카드]` `[전화]` `[이메일]` `[계좌]` | 번호 형식 개인정보 |
| `[본인사번]` `[본인이름]` | 로그인한 본인의 사번과 이름 |
| `[사번]` | 다른 직원의 사번 (조회는 거절, 요청 없이 자기소개만 하면 질문 없음) |

- 로그의 사용자 식별자는 사번이 아니라 가명 ID(`P-` + HMAC-SHA256 앞 12자리)예요.
- 본인 조회 답변(연차, 급여 등)은 로그에 저장하지 않아요 (`answer_masked`가 비어 있어요).
- 민감 주제 질문은 본문 대신 `[민감 주제 문의]`로, 기밀문서 질문은 `[기밀문서 질문]`으로 남겨요.

측정 결과 (직접 만든 가짜 사례 기준, `tests/masking_cases.py`)

| 항목 | 보강 전 | 보강 후 |
| --- | --- | --- |
| 가려야 하는 태그를 맞게 가림 | 88 / 99 | 99 / 99 |
| 가리면 안 되는 문장을 가림 | 2 / 54 | 0 / 54 |

사례에 없는 형태는 놓칠 수 있어요. 기준은 [마스킹 안내](./docs/masking.md), 사례별 결과는 [측정 결과](./docs/masking_eval.md)를 보세요.

## 아직 안 된 부분

| 항목 | 지금 | 할 일 | 담당 |
| --- | --- | --- | --- |
| 마스킹 (`guard`) | 태그 8종으로 가리고 측정 사례로 확인. 위 "마스킹" 절 참고 | 타인 이름·주소·병명은 가리지 않음 (팀 결정 필요). 글자로 풀어 쓴 번호는 못 잡음 | 진태현 |
| 의도 분류 (`classify`) | 규칙 먼저 + 분류 보조 LLM (위 "의도 분류"). 27B로 처음 보는 질문까지 측정 | 피해 호소에 "다른 사람"이 들어가면 규칙이 refuse로 정해 LLM을 안 부름. 최종 코드로 Colab 재측정 | 진태현 |
| 계산 (`calc`) | 출장비, 연차 일수, 시간외수당은 기준표로 계산(`CALC_TOOLS=on`일 때). 가족수당은 함수만 있음. LLM이 뽑는 계산 값은 종류 100%·값 88% | 계산 켜기 여부 결정, 가족수당·경조휴가 등 연결, 가정값(🔍) 항목은 값 확정 후 추가 | 진태현 |
| 검색 (`retrieve`) | 항 단위 `rag.chunks`를 키워드 + (선택) 벡터로 검색, 리랭커 없음 | 정한 방식 B(조문 검색 + 항 리랭크, RAG 저장소 `colab/hang_long_article_eval.ipynb`)를 backend에 반영. 의도분류 → 검색 2단계 평가 | 이재용 · 진태현 |
| 검증 (`verify`) | 검색 점수가 기준(0.3) 이상이면 통과. "규정에서 확인되지 않아요"로 시작하는 답변은 근거 없음으로 기록 | 키워드만 겹쳐도 통과해 근거 없이 답하는 경우가 있음. 재정렬 점수 기반으로 교체, 답변 숫자와 근거 대조 | 진태현 |
| 권한 | 코드에서 사번·역할을 토큰으로 고정, 기밀문서는 역할별 원본·가림·거부, 개인정보 테이블은 읽지 않음 | 챗봇 전용 계정(`chatbot_ro`), RLS | 박규태 · 진태현 |
| 로그 | HMAC-SHA256 가명 ID와 마스킹 질문·답변, 분류·근거·도구명·처리시간 저장. 조회 도구를 쓴 답변은 `answer_masked`를 비워 둠 | `.env`에 고정 `PSEUDO_KEY`를 안전하게 설정해야 재시작 후에도 가명 ID가 유지됨. "근거 없음"과 "권한 없음"을 구분하는 항목 추가 | 박규태 · 진태현 |
| 생애 이벤트 계획, 신청서 초안 | 없음 | 이후 | |

검색기는 `search(query, k) -> [{id, citation, revised, text, score}]` 형식만 지키면 파이프라인을 안 고치고 바꿔 끼울 수 있어요.
