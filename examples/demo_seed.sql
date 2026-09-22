BEGIN;

SELECT set_config('app.tenant_id', 'demo-tenant', true);
SELECT set_config('app.user_id', 'demo-sales-001', true);
SELECT set_config('app.scope_tags', 'sales:demo-sales-001,region:east', true);
SELECT set_config('app.is_admin', 'true', true);
SELECT set_config('app.session_id', 'seed', true);

INSERT INTO sales_employee (employee_id, tenant_id, display_name, department)
VALUES ('demo-sales-001', 'demo-tenant', '演示销售', '华东销售部')
ON CONFLICT (employee_id) DO UPDATE SET display_name = EXCLUDED.display_name;

INSERT INTO customer (
    customer_id, tenant_id, canonical_name, industry_code, permission_tags
) VALUES (
    'customer-east-001', 'demo-tenant', '华东智造集团', 'industrial',
    ARRAY['sales:demo-sales-001', 'region:east']
)
ON CONFLICT (customer_id) DO UPDATE SET canonical_name = EXCLUDED.canonical_name;

INSERT INTO customer_assignment (tenant_id, customer_id, employee_id, valid_from)
VALUES ('demo-tenant', 'customer-east-001', 'demo-sales-001', DATE '2026-01-01')
ON CONFLICT (customer_id, employee_id, valid_from) DO NOTHING;

INSERT INTO sales_contract (
    contract_id, tenant_id, customer_id, owner_employee_id,
    signed_at, amount, status, permission_tags
) VALUES
    (
        'HT-2026-001', 'demo-tenant', 'customer-east-001', 'demo-sales-001',
        DATE '2026-02-18', 1280000.00, 'active',
        ARRAY['sales:demo-sales-001', 'region:east']
    ),
    (
        'HT-2026-018', 'demo-tenant', 'customer-east-001', 'demo-sales-001',
        DATE '2026-06-12', 2150000.00, 'active',
        ARRAY['sales:demo-sales-001', 'region:east']
    ),
    (
        'HT-2026-031', 'demo-tenant', 'customer-east-001', 'demo-sales-001',
        DATE '2026-08-26', 1760000.00, 'active',
        ARRAY['sales:demo-sales-001', 'region:east']
    )
ON CONFLICT (contract_id) DO UPDATE SET amount = EXCLUDED.amount;

INSERT INTO contract_item (contract_id, product_id, quantity, line_amount)
VALUES
    ('HT-2026-001', 'product-vision', 1, 1280000.00),
    ('HT-2026-018', 'product-vision', 1, 2150000.00),
    ('HT-2026-031', 'product-ops', 1, 1760000.00)
ON CONFLICT (contract_id, product_id) DO UPDATE SET line_amount = EXCLUDED.line_amount;

-- 第二个销售 + 第二个客户（用于演示 RLS：非 admin 只能看到自己被分配的客户）
INSERT INTO sales_employee (employee_id, tenant_id, display_name, department)
VALUES ('demo-sales-002', 'demo-tenant', '演示销售二', '华南销售部')
ON CONFLICT (employee_id) DO UPDATE SET display_name = EXCLUDED.display_name;

INSERT INTO customer (
    customer_id, tenant_id, canonical_name, industry_code, permission_tags
) VALUES (
    'customer-south-002', 'demo-tenant', '华南精工科技', 'precision',
    ARRAY['sales:demo-sales-002', 'region:south']
)
ON CONFLICT (customer_id) DO UPDATE SET canonical_name = EXCLUDED.canonical_name;

INSERT INTO customer_assignment (tenant_id, customer_id, employee_id, valid_from)
VALUES ('demo-tenant', 'customer-south-002', 'demo-sales-002', DATE '2026-03-01')
ON CONFLICT (customer_id, employee_id, valid_from) DO NOTHING;

INSERT INTO sales_contract (
    contract_id, tenant_id, customer_id, owner_employee_id,
    signed_at, amount, status, permission_tags
) VALUES (
    'HT-2026-044', 'demo-tenant', 'customer-south-002', 'demo-sales-002',
    DATE '2026-09-01', 960000.00, 'active',
    ARRAY['sales:demo-sales-002', 'region:south']
)
ON CONFLICT (contract_id) DO UPDATE SET amount = EXCLUDED.amount;

INSERT INTO contract_item (contract_id, product_id, quantity, line_amount)
VALUES ('HT-2026-044', 'product-ops', 1, 960000.00)
ON CONFLICT (contract_id, product_id) DO UPDATE SET line_amount = EXCLUDED.line_amount;

-- 关系变更审批样例（配合 graph_change_request 的 candidate→confirm 闭环演示）
INSERT INTO graph_change_request (
    tenant_id, source_type, subject_id, relation_type, object_id,
    evidence, status, created_by
) VALUES (
    'demo-tenant', 'document_extraction',
    'customer-east-001', 'SIGNED', 'HT-2026-018',
    '{"source": "visit-note-2026-0912"}', 'pending', 'demo-sales-001'
)
ON CONFLICT DO NOTHING;

COMMIT;

