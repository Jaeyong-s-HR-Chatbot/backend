CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.conf_view_log (
    log_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_pseudo_id text NOT NULL,
    doc_id text NOT NULL,
    mode text NOT NULL,
    viewed_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);
