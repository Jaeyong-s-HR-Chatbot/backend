# 기밀문서 적재

PDF를 원본 조각(`conf.segments`)과 가린 검색용 사본(`conf.chunks_masked`)으로 분리합니다.
`sen`이 있는 조각의 검색용 본문은 `[민감정보]`로 저장합니다. 원본과 탐지 위치는 별도로 보존합니다.
역할과 문서 머리말을 기준으로 미리보기와 검색의 원문·가림·차단을 판단합니다.

- `CONF_PDF_DIR`: PDF 폴더. 비우면 작업 폴더의 `raw`를 사용합니다.
- `CONF_WORK_DIR`: 중간 결과 폴더. 비우면 `backend/data/conf`를 사용합니다.
- `NER_MODEL_DIR`: NER 모델 이름 또는 로컬 경로. 비우면 `soddokayo/klue-roberta-base-ner`를 사용합니다.
- `NLI_ENABLED`: `true`이면 발언별 NLI 기밀 판정을 켭니다. 기본값은 `false`입니다.
- `NLI_MODEL_DIR`: 미세조정한 mDeBERTa 모델의 로컬 폴더 경로입니다. NLI가 켜져 있을 때 필수입니다.
- `HRDB_URL`: 부서·직급별 인원 조회에 필요합니다. `--dry-run`에서도 읽기 전용으로 조회합니다.

관리자 승인 후 `scripts/create_conf_schema.sql`, `scripts/alter_conf_upload.sql`,
`scripts/alter_conf_nli.sql`을 적용하고 아래 명령으로 적재합니다.
**NLI 활성화 여부와 관계없이 적재 전에 `scripts/alter_conf_nli.sql` 적용이 필수입니다.**
기밀문서 미리보기와 검색에도 이 마이그레이션이 필요합니다.
기존 문서의 빈 업로드 정보는 별도 승인 후 `python -m scripts.backfill_conf_upload`로 채웁니다.
파일 크기는 `CONF_PDF_DIR`에서 확인하고 파일이 없으면 0으로 기록합니다.

```powershell
python -m confidential.ingest --dry-run
python -m confidential.ingest --doc <doc_id>
python -m confidential.ingest
python -m confidential.check_ingest
python -m confidential.check_ingest --names <저장소_밖_이름_목록>
```

`--dry-run`은 DB에 쓰지 않지만 **원본이 포함된 JSONL**을 작업 폴더에 저장합니다.
이 폴더와 모델 파일, PDF, 이름 목록은 Git에 추가하지 마세요. NER의 최대 입력 길이를 초과하는
조각은 마스킹을 생략하지 않고 처리 자체를 중단합니다. 각 문서의 DB 저장은 독립 트랜잭션이며,
같은 문서를 다시 적재하면 해당 문서의 기존 조각을 교체합니다.

문서 등급은 기본값이며, NLI를 활성화하면 NER·규칙 마스킹 후 원문 발언을 미세조정
모델로 분류합니다. 기밀로 판정된 발언은 조각 등급을 최소 L3로 올리고 담당자 검토
표시를 남깁니다. 노사협의회 녹취록의 기본 등급은 L2로 하되 NLI 판정 발언은 L3로
올립니다. 이는 근로자참여법 제16조의 회의 공개 원칙을 참고한 정책이며, 협의회
의결로 비공개할 수 있는 단서와 제17조의 비밀 유지 의무도 적용됩니다.

녹취록은 모든 발언의 NLI 판정이 성공한 경우에만 L2를 허용합니다. NLI가 꺼져 있거나
발언이 없거나 모델 로드·추론이 실패하면 L3 이상으로 처리하고, 실패 시 조각 전체에
담당자 검토 표시를 남깁니다. 다른 종류의 문서에서 NLI가 실패하면 적재를 중단합니다.
`conf.documents.nli_status`는 `not_run`, `success`, `failed` 중 하나이며, 성공 여부가
확인되지 않은 L2 녹취록은 열람·검색에서도 L3로 취급합니다. 판정 점수와 유형은
`conf.segments.nli_findings`에 저장하고, 검토 여부는 원문·검색 조각에 함께 저장합니다.
검토 표시가 있는 조각은 일반 masked 미리보기에서 전체 가리고 검색 후보에서 제외합니다.
마이그레이션 SQL은 관리자 승인 후에만 적용하세요.
TODO: 실제 운영에서는 노사 담당자가 NLI 판정을 검토·승인한 뒤에만 녹취록을 L2로 공개해야 합니다.

기밀문서 목록은 징계·노사 담당자와 시스템 관리자에게만, 업로드·삭제는 각 분류의
담당자에게만 허용합니다. 등록된 PDF만 업로드할 수 있으며 처리 중 재업로드·삭제는
409로 거절합니다. 업로드 파일은 `data/conf/uploads`에 보관합니다.
미리보기는 문서 분류 담당 역할에 원문을 허용하고, 허용 역할·참여자·대상자 여부를
문서별로 판단합니다. 시스템 관리자 단독 역할은 문서를 볼 수 없습니다.
L4 원문 열람 기록은 관리자 승인 후 `scripts/create_conf_view_log.sql`을 적용해야 저장됩니다.
기록에 실패해도 응답은 차단하지 않고 경고를 남깁니다. 원본 테이블의 DB 접근 권한은 별도로 제한해야 합니다.

회의 내용 질문은 권한이 있는 문서의 가린 사본을 검색한 뒤 열람 모드에 맞게 근거를 만듭니다.
가린 문서 카드의 제목은 개인정보 노출을 막기 위해 분야별 일반 제목으로 표시합니다.
기밀문서 답변은 로컬 LLM 또는 가짜 LLM으로만 생성하며, 로그에는 질문과 답변 대신
대체 문구를 기록합니다. 가린 사본에 이름이 없어 이름으로 찾는 검색은 제한됩니다.
TODO: 배포 서버에서는 localhost가 아닌 자체 LLM 서버도 허용할 수 있도록 주소 허용 목록을 설정합니다.
기존 조각의 임베딩 보완은 관리자 승인 후 실행합니다.

```powershell
python scripts/embed_conf.py --dry-run
python scripts/embed_conf.py
```

가린 본문을 `EMBED_MODEL`과 기본 Hugging Face 캐시를 이용해 정규화·1024차원 검증 후 저장합니다.
모델을 불러올 수 없으면 경고하고 임베딩 없이 저장하며, 검색은 키워드를 사용합니다.
