"""Purchase-order entry (PO integration): one confirmed-save path shared by the form, typed text and document drafts.

The model only DRAFTS; `store.save_po` is the only writer of purchase_orders / po_lines, and only the Save endpoint calls it.
"""
