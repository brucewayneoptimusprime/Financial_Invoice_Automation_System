-- Schema version 4 (rules settings with layered defaults). Added on top of schema.sql + schema_v2.sql + schema_v3.sql; no existing
-- table changes. Applied by init_db for a fresh database and by `python -m app.db.migrate` for a version-3 database.
-- Statements are separated by ";" at the end of a line (the migration runs them one by one inside ONE transaction).
-- The GLOBAL defaults stay where they always were (rules.params / rules.enabled / settings.confidence_threshold).

-- The per-PO override layer. NULL = inherits the global default. The ranges are enforced again here (defence in depth).
CREATE TABLE po_settings (
    po_id                  INTEGER PRIMARY KEY REFERENCES purchase_orders(id),
    tolerance_pct          REAL    CHECK (tolerance_pct IS NULL OR tolerance_pct BETWEEN 0 AND 25),
    tolerance_abs_minor    INTEGER CHECK (tolerance_abs_minor IS NULL OR tolerance_abs_minor BETWEEN 0 AND 100000000),
    tolerance_mode         TEXT    CHECK (tolerance_mode IS NULL OR tolerance_mode IN ('lesser_of', 'greater_of')),
    confidence_threshold   REAL    CHECK (confidence_threshold IS NULL OR confidence_threshold BETWEEN 0.5 AND 0.99),
    duplicate_days         INTEGER CHECK (duplicate_days IS NULL OR duplicate_days BETWEEN 0 AND 90),
    duplicate_amount_minor INTEGER CHECK (duplicate_amount_minor IS NULL OR duplicate_amount_minor BETWEEN 0 AND 1000000),
    updated_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Per-PO rule switches (no row = inherits the global switch). A locked rule can never be stored as off.
CREATE TABLE po_rule_switches (
    po_id      INTEGER NOT NULL REFERENCES purchase_orders(id),
    rule_id    TEXT    NOT NULL REFERENCES rules(id),
    enabled    INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    updated_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (po_id, rule_id),
    CHECK (enabled = 1 OR (rule_id <> 'r_duplicate_exact' AND rule_id <> 'r_vendor_status'))
);

-- The settings change log (owner decision 1: audit_events needs a run; a settings change has none). Never updated.
CREATE TABLE settings_events (
    id         INTEGER PRIMARY KEY,
    scope      TEXT NOT NULL CHECK (scope IN ('global', 'po')),
    po_id      INTEGER REFERENCES purchase_orders(id),
    key        TEXT NOT NULL,
    old_value  TEXT CHECK (old_value IS NULL OR json_valid(old_value)),
    new_value  TEXT CHECK (new_value IS NULL OR json_valid(new_value)),
    actor      TEXT NOT NULL DEFAULT 'unauthenticated demo user',
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK ((scope = 'global' AND po_id IS NULL) OR (scope = 'po' AND po_id IS NOT NULL))
);
CREATE INDEX idx_settings_events_po ON settings_events(po_id, id);
