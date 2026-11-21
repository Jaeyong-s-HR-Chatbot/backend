ALTER TABLE conf.documents
    ADD COLUMN IF NOT EXISTS file_name text,
    ADD COLUMN IF NOT EXISTS file_size bigint,
    ADD COLUMN IF NOT EXISTS uploaded_at timestamptz,
    ADD COLUMN IF NOT EXISTS uploaded_by text;
