-- Schema version 2 (line-item PO consumption). Added on top of schema.sql (version 1); no existing table changes.
-- Applied by init_db for a fresh database and by `python -m app.db.migrate` for a version-1 database.
-- Statements are separated by ";" at the end of a line (the migration runs them one by one inside ONE transaction).

-- How each ledger entry is allocated: to a PO line, or to the PO total unassigned (po_line_id NULL).
-- Invariant: for every ledger entry, SUM(po_consumption.amount) = ledger_entries.amount (same sign convention).
-- The PO balance is still total minus SUM(ledger_entries); this table never replaces it.
CREATE TABLE po_consumption (
    id               INTEGER PRIMARY KEY,
    ledger_entry_id  INTEGER NOT NULL REFERENCES ledger_entries(id),
    po_id            INTEGER NOT NULL REFERENCES purchase_orders(id),
    po_line_id       INTEGER REFERENCES po_lines(id),
    invoice_id       INTEGER NOT NULL REFERENCES invoices(id),
    invoice_line_id  INTEGER REFERENCES invoice_lines(id),
    run_id           TEXT REFERENCES runs(id),
    quantity         TEXT,
    amount           INTEGER NOT NULL,
    type             TEXT NOT NULL CHECK (type IN ('commit', 'reversal')),
    matched_by       TEXT NOT NULL CHECK (matched_by IN ('auto', 'manual_reviewer', 'legacy')),
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK ((type = 'commit' AND amount > 0) OR (type = 'reversal' AND amount < 0)),
    CHECK (po_line_id IS NOT NULL OR quantity IS NULL)
);
CREATE INDEX idx_consumption_po_line ON po_consumption(po_line_id);
CREATE INDEX idx_consumption_entry ON po_consumption(ledger_entry_id);

-- The line-matching result of a run, one row per invoice line, for the reviewer's line picker.
CREATE TABLE invoice_line_matches (
    id               INTEGER PRIMARY KEY,
    run_id           TEXT NOT NULL REFERENCES runs(id),
    invoice_id       INTEGER NOT NULL REFERENCES invoices(id),
    invoice_line_id  INTEGER NOT NULL UNIQUE REFERENCES invoice_lines(id),
    po_id            INTEGER NOT NULL REFERENCES purchase_orders(id),
    status           TEXT NOT NULL CHECK (status IN ('matched', 'ambiguous', 'no_match', 'not_evaluable')),
    po_line_id       INTEGER REFERENCES po_lines(id),
    score            REAL,
    candidates       TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(candidates)),
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_line_matches_run ON invoice_line_matches(run_id);
