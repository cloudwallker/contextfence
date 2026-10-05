-- Additive migration: old binaries insert explicit context columns and retain compatibility.
ALTER TABLE context_items ADD COLUMN retired_at timestamptz;
ALTER TABLE context_items ADD COLUMN retired_reason varchar(32);
ALTER TABLE context_items ADD CONSTRAINT context_items_retired_metadata
    CHECK ((retired_at IS NULL AND retired_reason IS NULL)
        OR (retired_at IS NOT NULL AND retired_reason IS NOT NULL AND retired_reason = 'DATABASE_RESTORE'));
ALTER TABLE context_items ADD CONSTRAINT context_items_retired_body
    CHECK (retired_at IS NULL OR content = '');
