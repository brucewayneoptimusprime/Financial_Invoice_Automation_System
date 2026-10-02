"""Purchase-order export (EXPORT_PLAN; SPEC section 11 item 90): PDF, Word, Excel and CSV, built from the same read models the screens
use (`po.views.po_list`, `po.views.po_detail`), so every exported number equals the displayed one. Read-only: no model, no cost,
no database write. The UI calls it "Export": the system never sends anything; the browser saves a file.
"""
