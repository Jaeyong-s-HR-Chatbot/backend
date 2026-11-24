ALTER TABLE conf.segments
    ADD COLUMN IF NOT EXISTS nli_findings jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS review_required boolean NOT NULL DEFAULT false;

ALTER TABLE conf.chunks_masked
    ADD COLUMN IF NOT EXISTS review_required boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN conf.segments.nli_findings IS 'NLI 문장별 유형과 점수';
COMMENT ON COLUMN conf.segments.review_required IS 'NLI 판정에 따른 담당자 검토 필요 여부';
COMMENT ON COLUMN conf.chunks_masked.review_required IS 'NLI 판정에 따른 담당자 검토 필요 여부';
