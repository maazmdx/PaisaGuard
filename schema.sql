-- PaisaGuard Core Financial Schema (SQLite with WAL mode)
-- Canonical Currency: Pure INTEGER paise across all source, ledger, and decision tables.
-- Zero floating-point money columns.

CREATE TABLE IF NOT EXISTS oms_orders (
    order_id TEXT PRIMARY KEY,
    business_tx_id TEXT,
    amount_paise INTEGER NOT NULL,
    currency TEXT DEFAULT 'INR',
    customer_id TEXT,
    source_payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT DEFAULT 'created'
);

CREATE TABLE IF NOT EXISTS razorpay_settlements (
    payment_id TEXT PRIMARY KEY,
    business_tx_id TEXT,
    order_id TEXT,
    payout_id TEXT,
    amount_paise INTEGER NOT NULL,
    fee_paise INTEGER NOT NULL,
    tax_paise INTEGER NOT NULL,
    net_paise INTEGER NOT NULL,
    currency TEXT DEFAULT 'INR',
    payment_method TEXT DEFAULT 'upi',
    source_payload_hash TEXT NOT NULL,
    settled_at TEXT NOT NULL,
    status TEXT DEFAULT 'settled'
);

CREATE TABLE IF NOT EXISTS bank_payout_credits (
    credit_id TEXT PRIMARY KEY,
    payout_id TEXT, -- Nullable to support unmatched direct bank credits
    utr_number TEXT NOT NULL,
    credit_amount_paise INTEGER NOT NULL,
    source_payload_hash TEXT NOT NULL,
    credited_at TEXT NOT NULL,
    account_tail TEXT,
    status TEXT DEFAULT 'credited'
);

CREATE TABLE IF NOT EXISTS webhook_events (
    event_id TEXT PRIMARY KEY,
    payment_id TEXT,
    source_payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    hmac_signature TEXT,
    received_at TEXT NOT NULL,
    status TEXT DEFAULT 'received'
);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_timestamp TEXT NOT NULL,
    matcher_version TEXT NOT NULL,
    input_snapshot_hash TEXT NOT NULL,
    git_sha TEXT DEFAULT 'local',
    total_business_tx INTEGER NOT NULL,
    total_source_records INTEGER NOT NULL,
    matched_count INTEGER NOT NULL,
    exception_count INTEGER NOT NULL,
    metrics_json TEXT NOT NULL,
    status TEXT NOT NULL
);

-- Reconciliation decisions are strictly append-only.
-- Identity is defined by polymorphic (subject_type, subject_id).
CREATE TABLE IF NOT EXISTS reconciliation_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_fingerprint TEXT UNIQUE NOT NULL,
    input_snapshot_hash TEXT NOT NULL,
    matcher_version TEXT NOT NULL,
    subject_type TEXT NOT NULL, -- 'BUSINESS_TX', 'PAYOUT', 'BANK_CREDIT'
    subject_id TEXT NOT NULL,   -- e.g. 'tx_ops_001', 'payout_aug_01', 'bank_cr_orphan_01'
    business_tx_id TEXT,
    order_id TEXT,
    payment_id TEXT,
    payout_id TEXT,
    credit_id TEXT,
    match_status TEXT NOT NULL, -- 'MATCHED', 'EXCEPTION', 'RULE_OVERRIDDEN'
    discrepancy_code TEXT,
    variance_paise INTEGER DEFAULT 0,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_decision_links (
    run_id INTEGER NOT NULL,
    decision_id INTEGER NOT NULL,
    PRIMARY KEY (run_id, decision_id),
    FOREIGN KEY (run_id) REFERENCES reconciliation_runs(run_id),
    FOREIGN KEY (decision_id) REFERENCES reconciliation_decisions(decision_id)
);

CREATE TABLE IF NOT EXISTS agent_investigations (
    investigation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    evidence_bundle_hash TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    root_cause TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_record_ids TEXT NOT NULL, -- JSON array of cited IDs
    evidence_summary TEXT,
    proposed_action TEXT NOT NULL,
    should_abstain INTEGER NOT NULL DEFAULT 0,
    abstention_reason TEXT,
    policy_status TEXT NOT NULL, -- 'POLICY_APPROVED', 'POLICY_REJECTED'
    policy_reason TEXT,
    raw_response TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (decision_id) REFERENCES reconciliation_decisions(decision_id)
);

CREATE TABLE IF NOT EXISTS human_approvals (
    approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    investigation_id INTEGER,
    action TEXT NOT NULL, -- 'APPROVE', 'REJECT', 'ESCALATE', 'OVERRIDE'
    reviewer TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (decision_id) REFERENCES reconciliation_decisions(decision_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resolved_rules (
    rule_id TEXT PRIMARY KEY,
    rule_type TEXT NOT NULL,    -- e.g. 'MDR_SURCHARGE'
    scope_field TEXT NOT NULL,  -- e.g. 'payment_method'
    scope_value TEXT NOT NULL,  -- e.g. 'corporate_card'
    action TEXT NOT NULL,
    exception_code TEXT NOT NULL,
    description TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT DEFAULT 'ACTIVE'
);

-- Rebuildable projection view for current decision state.
-- Derives current disposition from the latest human_approvals record without mutating reconciliation_decisions.
CREATE VIEW IF NOT EXISTS v_current_decisions AS
SELECT 
    d.decision_id,
    d.decision_fingerprint,
    d.input_snapshot_hash,
    d.matcher_version,
    d.subject_type,
    d.subject_id,
    d.business_tx_id,
    d.order_id,
    d.payment_id,
    d.payout_id,
    d.credit_id,
    d.match_status,
    d.discrepancy_code,
    d.variance_paise,
    d.evidence_json,
    d.created_at AS decision_created_at,
    COALESCE(ha.action, 'UNRESOLVED') AS current_disposition,
    ha.reviewer AS resolved_by,
    ha.notes AS resolution_notes,
    ha.created_at AS resolved_at
FROM reconciliation_decisions d
LEFT JOIN (
    SELECT decision_id, action, reviewer, notes, created_at,
           ROW_NUMBER() OVER(PARTITION BY decision_id ORDER BY approval_id DESC) as rn
    FROM human_approvals
) ha ON d.decision_id = ha.decision_id AND ha.rn = 1;

CREATE INDEX IF NOT EXISTS idx_oms_orders_tx ON oms_orders(business_tx_id);
CREATE INDEX IF NOT EXISTS idx_rp_settlements_tx ON razorpay_settlements(business_tx_id);
CREATE INDEX IF NOT EXISTS idx_rp_settlements_order ON razorpay_settlements(order_id);
CREATE INDEX IF NOT EXISTS idx_rp_settlements_payout ON razorpay_settlements(payout_id);
CREATE INDEX IF NOT EXISTS idx_bank_credits_payout ON bank_payout_credits(payout_id);
CREATE INDEX IF NOT EXISTS idx_recon_decisions_subject ON reconciliation_decisions(subject_type, subject_id);
CREATE INDEX IF NOT EXISTS idx_recon_decisions_fp ON reconciliation_decisions(decision_fingerprint);
CREATE INDEX IF NOT EXISTS idx_run_decision_links ON run_decision_links(run_id, decision_id);
CREATE INDEX IF NOT EXISTS idx_agent_inv_decision ON agent_investigations(decision_id);
CREATE INDEX IF NOT EXISTS idx_human_approvals_decision ON human_approvals(decision_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_agg ON audit_events(aggregate_type, aggregate_id);
