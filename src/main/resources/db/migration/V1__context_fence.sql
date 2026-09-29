CREATE TABLE tenant_guard (
    tenant varchar(64) PRIMARY KEY
);

CREATE TABLE source_state (
    tenant varchar(64) NOT NULL REFERENCES tenant_guard(tenant),
    source_id varchar(64) NOT NULL,
    last_sequence bigint NOT NULL CHECK (last_sequence > 0),
    content_version bigint NOT NULL CHECK (content_version > 0),
    auth_epoch bigint NOT NULL CHECK (auth_epoch > 0),
    state varchar(8) NOT NULL CHECK (state IN ('ACTIVE', 'DELETED')),
    content text NOT NULL CHECK (octet_length(content) <= 65536),
    content_hash char(64) NOT NULL,
    readers jsonb NOT NULL CHECK (jsonb_typeof(readers) = 'array' AND jsonb_array_length(readers) <= 100),
    fresh_until timestamptz NOT NULL,
    PRIMARY KEY (tenant, source_id)
);

CREATE TABLE source_events (
    tenant varchar(64) NOT NULL,
    source_id varchar(64) NOT NULL,
    sequence bigint NOT NULL CHECK (sequence > 0),
    payload_hash char(64) NOT NULL,
    result jsonb NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant, source_id, sequence),
    FOREIGN KEY (tenant, source_id) REFERENCES source_state(tenant, source_id)
);

CREATE TABLE context_items (
    tenant varchar(64) NOT NULL REFERENCES tenant_guard(tenant),
    id uuid NOT NULL,
    owner_subject varchar(64) NOT NULL,
    kind varchar(8) NOT NULL CHECK (kind IN ('SOURCE','DERIVED')),
    content text NOT NULL CHECK (octet_length(content) <= 65536),
    content_hash char(64) NOT NULL,
    parent_ids jsonb NOT NULL CHECK (jsonb_typeof(parent_ids) = 'array'),
    depth integer NOT NULL CHECK (depth BETWEEN 0 AND 4),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant, id)
);
CREATE INDEX context_items_owner ON context_items (tenant, owner_subject, id);

CREATE TABLE context_sources (
    tenant varchar(64) NOT NULL,
    context_id uuid NOT NULL,
    source_id varchar(64) NOT NULL,
    content_version bigint NOT NULL CHECK (content_version > 0),
    auth_epoch bigint NOT NULL CHECK (auth_epoch > 0),
    PRIMARY KEY (tenant, context_id, source_id),
    FOREIGN KEY (tenant, context_id) REFERENCES context_items(tenant, id),
    FOREIGN KEY (tenant, source_id) REFERENCES source_state(tenant, source_id)
);

CREATE TABLE admission_receipts (
    tenant varchar(64) NOT NULL REFERENCES tenant_guard(tenant),
    id uuid NOT NULL,
    owner_subject varchar(64) NOT NULL,
    checked_at timestamptz NOT NULL,
    decision varchar(40) NOT NULL,
    context_ids jsonb NOT NULL,
    source_versions jsonb NOT NULL,
    reasons jsonb NOT NULL,
    PRIMARY KEY (tenant, id)
);
CREATE INDEX admission_receipts_owner ON admission_receipts (tenant, owner_subject, id);
