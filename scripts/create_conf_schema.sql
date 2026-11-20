-- 기밀문서 원본과 검색용 가린 사본을 분리합니다. pgvector가 미리 설치되어 있어야 합니다.
CREATE SCHEMA IF NOT EXISTS conf;

CREATE TABLE IF NOT EXISTS conf.documents (
    doc_id text PRIMARY KEY,
    title text NOT NULL,
    doc_kind text NOT NULL,
    form text NOT NULL,
    category text NOT NULL,
    level text NOT NULL CHECK (level IN ('L1', 'L2', 'L3', 'L4')),
    allowed_roles text[] NOT NULL DEFAULT '{}',
    subject_emp_id text,
    participants jsonb NOT NULL DEFAULT '[]'::jsonb,
    pair_doc_id text,
    issued_at date,
    owner_dept text,
    doc_no text,
    confidential boolean NOT NULL DEFAULT true,
    source_file text NOT NULL,
    status text NOT NULL CHECK (status IN ('processing', 'ready', 'failed')),
    chunk_count integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS conf.segments (
    chunk_id text PRIMARY KEY,
    doc_id text NOT NULL REFERENCES conf.documents(doc_id),
    seq integer NOT NULL,
    chunk_type text NOT NULL,
    article text,
    page integer,
    speaker_code text,
    speaker_label text,
    text text NOT NULL,
    level text NOT NULL,
    entities jsonb NOT NULL,
    og jsonb NOT NULL,
    pos jsonb NOT NULL,
    qi jsonb NOT NULL,
    sen text
);

CREATE TABLE IF NOT EXISTS conf.chunks_masked (
    chunk_id text PRIMARY KEY REFERENCES conf.segments(chunk_id),
    doc_id text NOT NULL REFERENCES conf.documents(doc_id),
    seq integer NOT NULL,
    chunk_type text NOT NULL,
    article text,
    speaker_masked text,
    masked_text text NOT NULL,
    level text NOT NULL,
    sen text,
    allowed_roles text[] NOT NULL DEFAULT '{}',
    embedding vector(1024),
    tsv tsvector
);

COMMENT ON COLUMN conf.documents.doc_id IS '문서 식별자';
COMMENT ON COLUMN conf.documents.title IS '문서 제목';
COMMENT ON COLUMN conf.documents.doc_kind IS '문서 종류';
COMMENT ON COLUMN conf.documents.form IS '문서 양식';
COMMENT ON COLUMN conf.documents.category IS '문서 분류';
COMMENT ON COLUMN conf.documents.level IS '문서 보안 등급';
COMMENT ON COLUMN conf.documents.allowed_roles IS '문서에 허용된 역할';
COMMENT ON COLUMN conf.documents.subject_emp_id IS '문서 대상자 사번';
COMMENT ON COLUMN conf.documents.participants IS '사번과 역할로 구성된 참석자';
COMMENT ON COLUMN conf.documents.pair_doc_id IS '짝 문서 식별자';
COMMENT ON COLUMN conf.documents.issued_at IS '문서 일자';
COMMENT ON COLUMN conf.documents.owner_dept IS '문서 담당 부서';
COMMENT ON COLUMN conf.documents.doc_no IS '문서 번호';
COMMENT ON COLUMN conf.documents.confidential IS '기밀문서 여부';
COMMENT ON COLUMN conf.documents.source_file IS '원본 파일명';
COMMENT ON COLUMN conf.documents.status IS '처리 상태';
COMMENT ON COLUMN conf.documents.chunk_count IS '문서의 조각 수';
COMMENT ON COLUMN conf.documents.created_at IS '최초 생성 시각';
COMMENT ON COLUMN conf.segments.chunk_id IS '원본 조각 식별자';
COMMENT ON COLUMN conf.segments.doc_id IS '문서 식별자';
COMMENT ON COLUMN conf.segments.seq IS '문서 내 조각 순서';
COMMENT ON COLUMN conf.segments.chunk_type IS '조각 종류';
COMMENT ON COLUMN conf.segments.article IS '발언 또는 양식 항목';
COMMENT ON COLUMN conf.segments.page IS '원본 쪽 번호';
COMMENT ON COLUMN conf.segments.speaker_code IS '발언자 코드';
COMMENT ON COLUMN conf.segments.speaker_label IS '원본 발언자 표시';
COMMENT ON COLUMN conf.segments.text IS '검색하지 않는 원본 내용';
COMMENT ON COLUMN conf.segments.level IS '조각 보안 등급';
COMMENT ON COLUMN conf.segments.entities IS '본문 및 발언자 표시에서 찾은 이름과 식별정보 구간';
COMMENT ON COLUMN conf.segments.og IS '찾은 부서 구간';
COMMENT ON COLUMN conf.segments.pos IS '찾은 직급 구간';
COMMENT ON COLUMN conf.segments.qi IS '간접 식별 조합과 가림 여부';
COMMENT ON COLUMN conf.segments.sen IS '민감정보 종류';
COMMENT ON COLUMN conf.chunks_masked.chunk_id IS '원본 조각과 연결된 식별자';
COMMENT ON COLUMN conf.chunks_masked.doc_id IS '문서 식별자';
COMMENT ON COLUMN conf.chunks_masked.seq IS '문서 내 조각 순서';
COMMENT ON COLUMN conf.chunks_masked.chunk_type IS '조각 종류';
COMMENT ON COLUMN conf.chunks_masked.article IS '발언 또는 양식 항목';
COMMENT ON COLUMN conf.chunks_masked.speaker_masked IS '가린 발언자 표시';
COMMENT ON COLUMN conf.chunks_masked.masked_text IS '검색용 가린 내용';
COMMENT ON COLUMN conf.chunks_masked.level IS '조각 보안 등급';
COMMENT ON COLUMN conf.chunks_masked.sen IS '민감정보 종류';
COMMENT ON COLUMN conf.chunks_masked.allowed_roles IS '조각에 허용된 역할';
COMMENT ON COLUMN conf.chunks_masked.embedding IS '검색 임베딩, 이번 단계에서는 NULL';
COMMENT ON COLUMN conf.chunks_masked.tsv IS '가린 내용의 검색 색인용 벡터';

CREATE INDEX IF NOT EXISTS conf_segments_doc_idx ON conf.segments (doc_id);
CREATE INDEX IF NOT EXISTS conf_masked_doc_idx ON conf.chunks_masked (doc_id);
CREATE INDEX IF NOT EXISTS conf_masked_tsv_idx ON conf.chunks_masked USING gin (tsv);
