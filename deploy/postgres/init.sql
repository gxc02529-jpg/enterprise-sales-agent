CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS sales_employee (
    employee_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    display_name text NOT NULL,
    department text,
    active boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS customer (
    customer_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    canonical_name text NOT NULL,
    industry_code text,
    permission_tags text[] NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS customer_assignment (
    tenant_id text NOT NULL,
    customer_id text NOT NULL REFERENCES customer(customer_id),
    employee_id text NOT NULL REFERENCES sales_employee(employee_id),
    valid_from date NOT NULL DEFAULT current_date,
    valid_to date,
    PRIMARY KEY (customer_id, employee_id, valid_from)
);

CREATE TABLE IF NOT EXISTS sales_contract (
    contract_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    customer_id text NOT NULL REFERENCES customer(customer_id),
    owner_employee_id text NOT NULL REFERENCES sales_employee(employee_id),
    signed_at date,
    amount numeric(18, 2) NOT NULL CHECK (amount >= 0),
    status text NOT NULL,
    permission_tags text[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS contract_item (
    contract_id text NOT NULL REFERENCES sales_contract(contract_id),
    product_id text NOT NULL,
    quantity numeric(18, 4) NOT NULL DEFAULT 1 CHECK (quantity >= 0),
    line_amount numeric(18, 2) NOT NULL DEFAULT 0 CHECK (line_amount >= 0),
    PRIMARY KEY (contract_id, product_id)
);

CREATE TABLE IF NOT EXISTS document_metadata (
    document_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    title text NOT NULL,
    document_type text NOT NULL,
    customer_ids text[] NOT NULL DEFAULT '{}',
    permission_tags text[] NOT NULL DEFAULT '{}',
    content_hash text NOT NULL,
    ingestion_status text NOT NULL DEFAULT 'pending',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS document_ingestion_job (
    job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id text NOT NULL,
    document_id text NOT NULL,
    submitted_by text NOT NULL,
    submitter_roles text[] NOT NULL DEFAULT '{}',
    scope_tags text[] NOT NULL DEFAULT '{}',
    filename text NOT NULL,
    media_type text NOT NULL,
    metadata jsonb NOT NULL,
    content bytea,
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'parsing', 'indexing', 'completed', 'failed', 'dead_letter')),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    locked_by text,
    locked_at timestamptz,
    error_code text,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

ALTER TABLE document_ingestion_job
DROP CONSTRAINT IF EXISTS document_ingestion_job_status_check;
ALTER TABLE document_ingestion_job
ADD CONSTRAINT document_ingestion_job_status_check
CHECK (status IN ('queued', 'parsing', 'indexing', 'completed', 'failed', 'dead_letter'));

CREATE TABLE IF NOT EXISTS memory_record (
    memory_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id text NOT NULL,
    layer text NOT NULL CHECK (layer IN ('session', 'user', 'business')),
    owner_user_id text,
    session_id text,
    content text NOT NULL,
    entity_ids text[] NOT NULL DEFAULT '{}',
    permission_tags text[] NOT NULL DEFAULT '{}',
    status text NOT NULL CHECK (status IN ('candidate', 'active', 'rejected', 'deleted')),
    reviewed_by text,
    reviewed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    CHECK (layer <> 'user' OR owner_user_id IS NOT NULL),
    CHECK (layer <> 'business' OR status <> 'active' OR reviewed_by IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS graph_change_request (
    change_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id text NOT NULL,
    source_type text NOT NULL CHECK (source_type IN ('cdc', 'document_extraction', 'manual')),
    subject_id text NOT NULL,
    relation_type text NOT NULL,
    object_id text NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}',
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'rolled_back')),
    graph_version bigint,
    created_by text NOT NULL,
    reviewed_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    reviewed_at timestamptz
);

CREATE TABLE IF NOT EXISTS audit_event (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    request_id text NOT NULL,
    session_id text,
    event_type text NOT NULL,
    tool_name text,
    input_digest text,
    output_digest text,
    status text NOT NULL,
    elapsed_ms integer,
    token_usage jsonb NOT NULL DEFAULT '{}',
    metadata jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_contract_owner_date ON sales_contract (tenant_id, owner_employee_id, signed_at);
CREATE INDEX IF NOT EXISTS idx_assignment_employee ON customer_assignment (tenant_id, employee_id, customer_id);
CREATE INDEX IF NOT EXISTS idx_memory_recall ON memory_record (tenant_id, layer, owner_user_id, status);
CREATE INDEX IF NOT EXISTS idx_audit_request ON audit_event (tenant_id, request_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_graph_review ON graph_change_request (tenant_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_ingestion_queue
ON document_ingestion_job (status, created_at)
WHERE status IN ('queued', 'parsing', 'indexing');

ALTER TABLE customer ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_contract ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_metadata ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_ingestion_job ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_record ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer FORCE ROW LEVEL SECURITY;
ALTER TABLE sales_contract FORCE ROW LEVEL SECURITY;
ALTER TABLE document_metadata FORCE ROW LEVEL SECURITY;
ALTER TABLE document_ingestion_job FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_record FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS customer_scope_policy ON customer;
CREATE POLICY customer_scope_policy ON customer
USING (
    tenant_id = current_setting('app.tenant_id', true)
    AND (
        current_setting('app.is_admin', true) = 'true'
        OR EXISTS (
            SELECT 1 FROM customer_assignment ca
            WHERE ca.customer_id = customer.customer_id
              AND ca.employee_id = current_setting('app.user_id', true)
              AND (ca.valid_to IS NULL OR ca.valid_to >= current_date)
        )
    )
);

DROP POLICY IF EXISTS contract_scope_policy ON sales_contract;
CREATE POLICY contract_scope_policy ON sales_contract
USING (
    tenant_id = current_setting('app.tenant_id', true)
    AND (current_setting('app.is_admin', true) = 'true'
         OR owner_employee_id = current_setting('app.user_id', true))
);

DROP POLICY IF EXISTS document_scope_policy ON document_metadata;
CREATE POLICY document_scope_policy ON document_metadata
USING (
    tenant_id = current_setting('app.tenant_id', true)
    AND (current_setting('app.is_admin', true) = 'true'
         OR permission_tags && string_to_array(current_setting('app.scope_tags', true), ','))
);

DROP POLICY IF EXISTS ingestion_job_scope_policy ON document_ingestion_job;
CREATE POLICY ingestion_job_scope_policy ON document_ingestion_job
USING (
    current_setting('app.ingestion_worker', true) = 'true'
    OR (
        tenant_id = current_setting('app.tenant_id', true)
        AND current_setting('app.is_admin', true) = 'true'
    )
)
WITH CHECK (
    current_setting('app.ingestion_worker', true) = 'true'
    OR (
        tenant_id = current_setting('app.tenant_id', true)
        AND current_setting('app.is_admin', true) = 'true'
    )
);

DROP POLICY IF EXISTS memory_scope_policy ON memory_record;
CREATE POLICY memory_scope_policy ON memory_record
USING (
    tenant_id = current_setting('app.tenant_id', true)
    AND (
        current_setting('app.is_admin', true) = 'true'
        OR (layer = 'user' AND owner_user_id = current_setting('app.user_id', true))
        OR (layer = 'session'
            AND session_id = current_setting('app.session_id', true))
        OR (layer = 'business'
            AND permission_tags && string_to_array(current_setting('app.scope_tags', true), ','))
    )
);
