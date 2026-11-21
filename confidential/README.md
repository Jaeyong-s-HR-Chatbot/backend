# 기밀문서 적재

PDF를 원본 조각(`conf.segments`)과 가린 검색용 사본(`conf.chunks_masked`)으로 분리합니다.
`sen`이 있는 조각의 검색용 본문은 `[민감정보]`로 저장합니다. 원본과 탐지 위치는 별도로 보존합니다.
검색 연결과 문장별 등급 판정은 포함하지 않습니다.

- `CONF_PDF_DIR`: PDF 폴더. 비우면 작업 폴더의 `raw`를 사용합니다.
- `CONF_WORK_DIR`: 중간 결과 폴더. 비우면 `backend/data/conf`를 사용합니다.
- `NER_MODEL_DIR`: NER 모델 이름 또는 로컬 경로. 비우면 `soddokayo/klue-roberta-base-ner`를 사용합니다.
- `HRDB_URL`: 부서·직급별 인원 조회에 필요합니다. `--dry-run`에서도 읽기 전용으로 조회합니다.

관리자 승인 후 `scripts/create_conf_schema.sql`과 `scripts/alter_conf_upload.sql`을 적용하고 아래 명령으로 적재합니다.
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

기밀문서 목록은 징계·노사 담당자와 시스템 관리자에게만, 업로드·삭제는 각 분류의
담당자에게만 허용합니다. 등록된 PDF만 업로드할 수 있으며 처리 중 재업로드·삭제는
409로 거절합니다. 업로드 파일은 `data/conf/uploads`에 보관합니다. 문서 내용 조회와
검색 연결은 아직 제공하지 않으며 원본 테이블의 DB 접근 권한은 별도로 제한해야 합니다.
