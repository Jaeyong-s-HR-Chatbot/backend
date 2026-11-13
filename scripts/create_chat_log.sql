CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.chat_log (
  log_id          BIGSERIAL PRIMARY KEY,
  user_pseudo_id  TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  question_masked TEXT NOT NULL,
  answer_masked   TEXT,
  pii_found       JSONB NOT NULL DEFAULT '{}',
  intent          TEXT,
  kind            TEXT,
  cited_chunks    JSONB NOT NULL DEFAULT '[]',
  tools_used      JSONB NOT NULL DEFAULT '[]',
  latency_ms      INTEGER
);

CREATE INDEX IF NOT EXISTS chat_log_created_idx ON app.chat_log (created_at);
