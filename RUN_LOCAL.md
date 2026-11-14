# Windows 로컬 HR 챗봇 시연 (Docker 없이)

내 컴퓨터에서 **PostgreSQL(인사·규정 DB) + Ollama(LLM) + 백엔드 + 프론트**를 띄워 시연하는 순서예요.
모든 데이터는 가상 데이터예요.

## 0. 한 번만 하는 준비

### 0-1. DB 올리기 (PostgreSQL 16 + pgvector가 설치돼 있어야 해요)

`backend` 폴더에서 PowerShell을 열고 실행해요. `postgres` 계정 비밀번호를 물어봐요.

```powershell
cd backend                      # hr_chatbot 폴더에서
.\.venv\Scripts\Activate.ps1
python -X utf8 -m pip install -r requirements.txt
python -X utf8 scripts\load_hrdb.py --db-main "..\DB-main\DB-main"
```

- 끝나면 `직원 800명, 문서 46건, 검색 대상 조각 2583개, 임베딩 2583개, 대화 로그 테이블 있음`이 나오고, `.env`에 `HRDB_URL`이 자동으로 들어가요.
- `hrdb`가 이미 있으면 멈춰요. 지우고 다시 올리려면 끝에 `--reset`을 붙여요.
- 계정이나 포트가 다르면 `--user`, `--port`를 붙여요. 예: `--user postgres --port 5433`
- **pgvector 확장이 없다고 나오면** PostgreSQL 버전에 맞는 pgvector를 먼저 설치해야 해요.
- **psql을 못 찾는다고 나오면** `--psql "C:\Program Files\PostgreSQL\16\bin\psql.exe"`처럼 경로를 알려 줘요.

### 0-2. Ollama 모델

```powershell
ollama list            # qwen3.5:4b 가 있어야 해요. 없으면: ollama pull qwen3.5:4b
```

`.env`의 LLM 설정은 이렇게 둬요. LLM 없이 흐름만 보려면 `LLM_BASE_URL`을 비워요(가짜 LLM).

```
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen3.5:4b
LLM_API_KEY=ollama
```

로컬 4GB GPU에서는 모델이 CPU와 나눠 실행되어 느릴 수 있어요. `ollama ps`의 `PROCESSOR` 열에서 CPU/GPU 사용을 확인하세요. 로컬 실행은 기능 확인용이며, 응답 시간은 서버 환경에서 측정해요.

## 1. 실행 순서 (시연할 때마다)

1. **PostgreSQL**: Windows 서비스로 켜져 있으면 그대로 돼요. (서비스 앱에서 `postgresql-x64-16` 실행 중인지 확인)
2. **Ollama**: 앱 실행
3. **백엔드** (새 PowerShell)
   ```powershell
   cd backend                      # hr_chatbot 폴더에서
   .\.venv\Scripts\Activate.ps1
   python -X utf8 -m uvicorn app.main:app --port 8000
   ```
   http://localhost:8000/health 에서 확인해요.
   - `db.ok: true`, `employees: 800` → DB 연결됨
   - `llm: qwen3.5:4b` → 실제 모델 사용 (`fake`면 가짜 LLM)
   - `search: keyword` 또는 `hybrid` → 검색 방식
4. **프론트** (다른 PowerShell)
   ```powershell
   cd frontend-main\frontend-main    # hr_chatbot 폴더에서
   npm run dev
   ```
   http://localhost:5173 을 열어요. 프론트 `.env`는 `VITE_API_URL=http://localhost:8000`, `VITE_USE_MOCK=false`여야 해요.

## 2. 로그인 계정 (DB의 가상 직원)

비밀번호는 모두 DB-main README에 적힌 테스트 비밀번호(`generate.py`의 `TEST_PASSWORD`)예요.

| 로그인 ID | 이름 | 소속 | 역할 | 시연 포인트 |
| --- | --- | --- | --- | --- |
| `e1584` | 이서준 | 신호팀 대리 | 임직원 | 결혼 예정, 결혼 휴가·경조금 신청 대기 중 |
| `e1412` | 박지현 | 고객서비스팀 과장 | 임직원 | 출산휴가 중 |
| `e1377` | 최도윤 | 차량1사업소 과장 | 임직원 | 교대근무, 둘째 출생 |
| `e1265` | 정하은 | 재무팀 차장 | 임직원 | 부친상 |
| `e1003` | 송정식 | 인사팀 부장 | 인사팀 | 인사팀 화면 |

화면의 입력 칸 예시(`E2023041`)는 예전 목업 사번이라 DB에 없어요. 위 로그인 ID를 써요.

## 3. 시연 질문

| 질문 | 기대 결과 |
| --- | --- |
| 내 연차 몇 개 남았어? | DB의 본인 연차 (이서준: 16일 중 5일 사용, 11일 남음) |
| 내 복지포인트 얼마 남았어? | DB의 본인 포인트 |
| 이번 달 내 급여 얼마 나왔어? | DB의 최근 급여 |
| 내 결혼 휴가 신청 결재됐어? | 본인 신청 현황 (대기) |
| 병가 쓸 때 진단서 필요해? | 취업규칙 제25조(병가) 근거 |
| 다음 달 결혼하는데 휴가 며칠이야? | 취업규칙 제26조(청원휴가), 별표 3 근거 |
| E1412 연차 몇 개 남았어? | 거절 (다른 직원) |
| 박지현 과장 연봉 얼마야? | 거절 (다른 직원) |
| 팀장한테 폭언을 들었어 | 고충처리 안내 |
| 오늘 점심 메뉴 추천해줘 | 거절 (근거 없음) |

다른 계정으로 로그인해서 "내 연차 몇 개 남았어?"를 물으면 그 사람 값이 나오는 것도 보여 주기 좋아요.

## 4. 검색 방식

- **keyword (기본)**: 추가 설치 없이 동작해요. 질문 단어와 용어 사전(`app/data/glossary.csv`)으로 조문을 찾아요. 말이 많이 다르면 못 찾을 수 있어요.
- **hybrid (벡터 + 키워드)**: DB에 들어 있는 BGE-M3 임베딩을 써요. 질문도 같은 모델로 벡터로 바꿔야 해서 아래를 설치해요(모델 약 2GB, 처음 질문 때 내려받아요).
  ```powershell
  python -X utf8 -m pip install sentence-transformers
  ```
  설치 후 백엔드를 다시 켜면 `/health`의 `search`가 `hybrid`로 바뀌어요.

### rag 패키지로 검색하기 (선택)

`rag-main` 폴더의 검색 패키지(Dense + BM25 → RRF, 선택 재정렬)를 쓰려면:

```powershell
python -X utf8 -m pip install numpy kiwipiepy      # BM25만 쓸 때. Dense까지 쓰려면 sentence-transformers도 설치
```

`.env`에 아래를 넣고 백엔드를 다시 켜요. `/health`의 `search`가 `rag`로 바뀌어요.

```
SEARCH_MODE=rag
RAG_EMBED=off        # GPU 메모리가 작으면 off (BM25만). on이면 BGE-M3를 불러와요
```

- 처음 켤 때 조각을 읽고 색인을 만드느라 20초쯤 걸려요.
- 패키지를 못 불러오면 경고를 남기고 기본 검색으로 동작해요.
- `RAG_EMBED=off`(BM25만)일 때는 범위 밖 질문을 "근거 없음"으로 거절하지 못해요. 거절까지 시연하려면 기본 검색(`SEARCH_MODE=auto`)을 쓰거나 Dense를 켜요.

## 5. 테스트

```powershell
python -X utf8 -m pytest -q tests\test_api.py       # DB·LLM 없이 가짜 모드
$env:HRDB_URL = (Select-String -Path .env -Pattern '^HRDB_URL=').Line.Substring(9)
python -X utf8 -m pytest -q tests\test_db.py        # 실제 DB에 붙여 확인
```

## 6. 끄기

백엔드와 프론트는 각 터미널에서 `Ctrl+C`. Ollama 모델만 메모리에서 내릴 때는 `ollama stop qwen3.5:4b`.

## 자주 발생하는 문제

- **/health의 db.ok가 false**: PostgreSQL 서비스가 꺼져 있거나 `.env`의 `HRDB_URL` 비밀번호·포트가 달라요. `scripts\load_hrdb.py`를 `--reset` 없이 다시 실행하면 접속 여부를 바로 알려 줘요.
- **로그인이 안 됨**: 로그인 ID는 `e1584`처럼 소문자 e + 숫자 4자리예요. `HRDB_URL`이 비어 있으면 가짜 계정(E1584, E1003)만 돼요.
- **답변이 오래 걸림**: 노트북에서는 조문 5개를 읽는 데 시간이 걸려요. `.env`의 `LLM_TIMEOUT`을 늘리거나, 흐름만 볼 때는 `LLM_BASE_URL`을 비워 가짜 LLM으로 돌려요.
- **Ollama에 연결할 수 없음**: Ollama 앱이 켜져 있는지 확인하고 http://localhost:11434/api/tags 에 접속해 봐요.
- **나중에 서버(vLLM)로 옮길 때**: `.env`의 `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`와 `HRDB_URL`만 바꾸면 돼요.
