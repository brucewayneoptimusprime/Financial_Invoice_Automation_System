-- Schema for SPEC section 5. Version tracked via PRAGMA user_version (see init_db.py).
--
-- Conventions:
--  * All money columns are INTEGER minor units (cents). Convert only via app.money.
--  * quantity / unit_price are decimal TEXT (never summed in SQL).
--  * PO balance is NEVER stored: it is derived from ledger_entries (see app.db.queries).
--  * Ledger sign convention: commit > 0, reversal < 0, so balance = total - SUM(amount).
--  * Timestamps are UTC ISO-8601 TEXT.

CREATE TABLE vendors (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    aliases     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(aliases)),
    tax_id      TEXT,
    country     TEXT,
    status      TEXT NOT NULL CHECK (status IN ('approved', 'new', 'blocked')),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE purchase_orders (
    id            INTEGER PRIMARY KEY,
    po_number     TEXT NOT NULL UNIQUE,
    vendor_id     INTEGER NOT NULL REFERENCES vendors(id),
    currency      TEXT NOT NULL,
    total_amount  INTEGER NOT NULL CHECK (total_amount >= 0),
    issued_date   TEXT,
    status        TEXT NOT NULL CHECK (status IN ('open', 'partially_billed', 'fully_billed', 'closed')),
    meta          TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(meta))
);

CREATE TABLE po_lines (
    id           INTEGER PRIMARY KEY,
    po_id        INTEGER NOT NULL REFERENCES purchase_orders(id),
    line_no      INTEGER NOT NULL,
    description  TEXT,
    quantity     TEXT,
    unit_price   TEXT,
    amount       INTEGER,
    UNIQUE (po_id, line_no)
);

CREATE TABLE runs (
    id              TEXT PRIMARY KEY,
    source_file     TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    started_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    finished_at     TEXT,
    -- The system's ORIGINAL decision. Never modified after the run completes.
    -- Human resolutions live in review_queue.resolution and invoices.status.
    final_decision  TEXT CHECK (final_decision IN ('approve', 'review', 'request_info', 'reject')),
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL NOT NULL DEFAULT 0,  -- telemetry, not ledger money; sub-cent precision needed
    model           TEXT
);

CREATE TABLE invoices (
    id              INTEGER PRIMARY KEY,
    run_id          TEXT REFERENCES runs(id),  -- NULL for seeded historic invoices
    vendor_id       INTEGER REFERENCES vendors(id),
    invoice_number  TEXT,
    invoice_date    TEXT,
    currency        TEXT,
    subtotal        INTEGER,
    tax             INTEGER,
    total           INTEGER,
    po_id           INTEGER REFERENCES purchase_orders(id),
    -- decision = the system's decision at run time. status = EFFECTIVE outcome, which a human
    -- resolution may change (e.g. decision 'review' -> status 'approved').
    decision        TEXT CHECK (decision IN ('approve', 'review', 'request_info', 'reject')),
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'in_review', 'awaiting_info', 'rejected')),
    source_file     TEXT,
    file_hash       TEXT,
    extracted       TEXT CHECK (extracted IS NULL OR json_valid(extracted)),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_invoices_file_hash ON invoices(file_hash);
CREATE INDEX idx_invoices_vendor_number ON invoices(vendor_id, invoice_number);
CREATE INDEX idx_invoices_po ON invoices(po_id);

CREATE TABLE invoice_lines (
    id           INTEGER PRIMARY KEY,
    invoice_id   INTEGER NOT NULL REFERENCES invoices(id),
    line_no      INTEGER NOT NULL,
    description  TEXT,
    quantity     TEXT,
    unit_price   TEXT,
    amount       INTEGER,
    UNIQUE (invoice_id, line_no)
);

-- Source of truth for PO consumption.
CREATE TABLE ledger_entries (
    id          INTEGER PRIMARY KEY,
    po_id       INTEGER NOT NULL REFERENCES purchase_orders(id),
    invoice_id  INTEGER NOT NULL REFERENCES invoices(id),
    amount      INTEGER NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('commit', 'reversal')),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK ((type = 'commit' AND amount > 0) OR (type = 'reversal' AND amount < 0))
);
CREATE INDEX idx_ledger_po ON ledger_entries(po_id);

-- rule_id has no FK on purpose: audit history must survive a rule being deleted.
CREATE TABLE audit_events (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id),
    seq         INTEGER NOT NULL,
    stage       TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    rule_id     TEXT,
    outcome     TEXT NOT NULL CHECK (outcome IN ('pass', 'flag', 'fail', 'info')),
    message     TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail)),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (run_id, seq)
);

CREATE TABLE rules (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    type                TEXT NOT NULL,
    params              TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(params)),
    -- 0 (approve) is not a valid trigger severity: rules may only escalate.
    severity_on_trigger INTEGER NOT NULL CHECK (severity_on_trigger >= 1),
    source              TEXT NOT NULL CHECK (source IN ('builtin', 'user', 'nl')),
    enabled             INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    original_text       TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK (source <> 'nl' OR original_text IS NOT NULL)
);

CREATE TABLE review_queue (
    id           INTEGER PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs(id),
    reason       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    resolution   TEXT CHECK (resolution IN ('approved', 'rejected')),
    resolved_at  TEXT
);

CREATE TABLE drafts (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id),
    kind        TEXT NOT NULL CHECK (kind IN ('vendor_email', 'notification')),
    "to"        TEXT,
    subject     TEXT,
    body        TEXT,
    status      TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'marked_sent')),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Non-rule runtime values only (confidence threshold, model override). Tolerances live in rule params.
CREATE TABLE settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL CHECK (json_valid(value))
);
