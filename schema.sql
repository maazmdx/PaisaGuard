-- PaisaGuard Core Financial Schema (SQLite with WAL mode)

CREATE TABLE IF NOT EXISTS oms_orders (
    order_id TEXT PRIMARY KEY,
    gross_amount REAL NOT NULL,
    currency TEXT DEFAULT 'INR',
    customer_id TEXT,
    created_at TEXT NOT NULL,
    status TEXT DEFAULT 'created'
);

CREATE TABLE IF NOT EXISTS razorpay_settlements (
    payment_id TEXT PRIMARY KEY,
    settlement_id TEXT,
    order_id TEXT,
    amount REAL NOT NULL,
    fee REAL NOT NULL,
    tax REAL NOT NULL,
    net_amount REAL NOT NULL,
    currency TEXT DEFAULT 'INR',
    payment_method TEXT DEFAULT 'upi',
    settled_at TEXT NOT NULL,
    status TEXT DEFAULT 'settled'
);

CREATE TABLE IF NOT EXISTS resolved_rules (
    rule_id TEXT PRIMARY KEY,
    pattern_key TEXT NOT NULL,
    action TEXT NOT NULL,
    exception_code TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gst_monthly_invoices (
    invoice_id TEXT PRIMARY KEY,
    month TEXT NOT NULL,
    total_taxable_value REAL NOT NULL,
    cgst REAL NOT NULL,
    sgst REAL NOT NULL,
    igst REAL NOT NULL,
    total_gst REAL NOT NULL,
    invoice_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliation_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT,
    payment_id TEXT,
    reconciled_status TEXT NOT NULL, -- 'MATCHED', 'EXCEPTION', 'RULE_OVERRIDDEN'
    order_amount REAL,
    settlement_amount REAL,
    expected_fee REAL,
    actual_fee REAL,
    fee_variance REAL,
    expected_tax REAL,
    actual_tax REAL,
    tax_variance REAL,
    sub_paise_drift REAL DEFAULT 0.0,
    exception_code TEXT,
    exception_msg TEXT,
    reconciled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_timestamp TEXT NOT NULL,
    total_audited INTEGER NOT NULL,
    matched_count INTEGER NOT NULL,
    exception_count INTEGER NOT NULL,
    match_rate REAL NOT NULL,
    sub_paise_accumulator REAL NOT NULL,
    gst_daily_aggregate REAL NOT NULL,
    gst_monthly_invoice REAL NOT NULL,
    gst_tax_leakage REAL NOT NULL,
    status TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_oms_orders_created ON oms_orders(created_at);
CREATE INDEX IF NOT EXISTS idx_rp_settlements_order_id ON razorpay_settlements(order_id);
CREATE INDEX IF NOT EXISTS idx_rp_settlements_settled ON razorpay_settlements(settled_at);
CREATE INDEX IF NOT EXISTS idx_recon_ledger_status ON reconciliation_ledger(reconciled_status);
CREATE INDEX IF NOT EXISTS idx_recon_runs_ts ON reconciliation_runs(run_timestamp);
