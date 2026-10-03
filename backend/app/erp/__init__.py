"""Simulated ERP purchase-order feed (ERP_PLAN; SPEC section 11 item 97). Labelled "Simulated ERP (demo)" everywhere.

source.py reads the bundled feed file; adapters/ map one ERP format each into the PO form's own input; preview.py classifies
(read-only); importer.py saves the ticked POs through the single PO writer. No model is involved anywhere.
"""
LABEL = "Simulated ERP (demo)"
