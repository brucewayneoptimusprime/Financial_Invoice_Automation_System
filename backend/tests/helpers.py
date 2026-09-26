SEED_TABLES = ["vendors", "purchase_orders", "po_lines", "invoices", "invoice_lines", "ledger_entries", "po_consumption"]


def snapshot(conn, tables):
    """Full ordered contents of the given tables, for exact state comparisons."""
    return {t: [tuple(r) for r in conn.execute(f'SELECT * FROM "{t}" ORDER BY 1')] for t in tables}
