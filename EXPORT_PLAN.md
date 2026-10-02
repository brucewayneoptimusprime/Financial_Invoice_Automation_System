# EXPORT PLAN: export purchase orders (awaiting approval)

2026-10-02, branch `feature/po-export` (created from `feature/gmail-integration` at `81cab06`). `master` and `feature/gmail-integration` stay unchanged, and nothing is pushed. No application code is written until this plan is approved.

**Feature.** Purchase orders can be exported as PDF, Word (.docx), Excel (.xlsx) or CSV. The UI always says **Export** with a download icon, never "Share": the system never sends anything; the browser saves a file.

**Principles (decided).**
- **Same data as the screen.** Files are built on the backend from the same read models the screens use: `po.views.po_list` (with its `q` / `status` / `currency` filters) and `po.views.po_detail`. Every exported number is therefore the value the screen shows.
- **No LLM, no cost, no database writes.**
- **Behind `ACCESS_TOKEN`.** The routes sit behind the token. The frontend fetches the file (with the token) and saves it, because a plain link cannot carry the token.
- **Money.** It comes from the existing integer-cents path (`money.from_minor` → exact `Decimal`), never floats.
- **Safe output.** CSV and Excel text cells are escaped against formula injection, and file names are sanitised.

## 1. Modules, routes and UI

**Backend:**

| Module | Role |
|---|---|
| `app/po/export/model.py` | Builds ONE neutral document model from the views: `ExportDoc(title, meta[(label, value)], sections[Section])`. A `Section` is either a key/value list or a table with typed columns (`text`, `money`, `qty`, `int`, `date`). Each renderer reads only this model, so all four formats carry identical values |
| `app/po/export/csv_render.py`, `xlsx_render.py`, `docx_render.py`, `pdf_render.py` | `render(doc) -> bytes`, one renderer per format (stdlib `csv`, openpyxl, python-docx, reportlab platypus) |
| `app/po/export/safety.py` | `safe_cell(text)` (formula-injection escaping), `safe_filename(...)`, the caps |
| `app/api/routes_po.py` | Two read-only routes (below) |

**Routes:** one design for all three entry points, both levels and all four formats. Both are GETs, both sit behind `ACCESS_TOKEN`, and both return the file with `Content-Disposition: attachment`.
- `GET /api/pos/export?format=pdf|docx|xlsx|csv[&q=&status=&currency=][&ids=1,2,3]`: **the summary (entry point 1).**
  - With `ids`, exactly those POs are exported. They must exist (404 naming the unknown ids); the filter values are still recorded, for the scope line.
  - Without `ids`, it exports `po_list(q, status, currency)`, i.e. what the list shows.
- `GET /api/pos/{id}/export?format=pdf|docx|xlsx|csv&level=financial|full`: **one PO (entry points 2 and 3).**
- **Errors:** 404 for an unknown PO; 422 for a bad format or level, or for more POs than the cap (section 3).
- **Media types:**
  - `application/pdf`
  - `application/vnd.openxmlformats-officedocument.wordprocessingml.document`
  - `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`
  - `text/csv; charset=utf-8`
- **File names** (sanitised to `[A-Za-z0-9._-]`, `po-<id>` when nothing is left, at most 80 characters, with an RFC 5987 `filename*` too):
  - summary: `purchase-orders-20261002-1415.xlsx`;
  - detail: `PO-SS-001-financial-20261002-1415.pdf`.

**Frontend:**
- `src/download.ts`: `downloadFile(url)`. It calls `apiFetch`, so the token is attached, then turns the response into a Blob, an object URL and an `<a download>` click, and revokes the URL. The file name comes from `Content-Disposition`. A 401 opens the existing token dialog.
- `components/POExportButton.tsx`: **the one reusable component for entry points 2 and 3.**
  - It is a small **Export** button with a download icon that opens a menu: **Level**, Financial / Full (with metadata), and **Format**, PDF / Word / Excel / CSV. Choosing a format downloads.
  - Props: `poId`, `poNumber`, `size="small" | "normal"`. The component and its behaviour are identical in the row and on the page.
  - It is a real `<button>` with `aria-expanded`, Escape closes the menu, and focus returns to the button.
- `components/POListExport.tsx`: the summary Export button on the Purchase orders page, with the same format menu and no level.
- `screens/POList.tsx`: a tick-box column with a "select all shown" header box (section 3), and a `POExportButton size="small"` at the far right of every row.
- `screens/PODetail.tsx`: a `POExportButton` at the top of the page, next to the title.

## 2. File contents and layout

**Header lines** in every file, from `ExportDoc.meta`:
- Exported from: Invoice Agent.
- Generated (UTC): `2026-10-02 14:15`.
- What: "Purchase orders (summary)" or "Purchase order PO-SS-001: Financial | Full".
- Summary only, the scope: "Ticked: 3 of 12 shown" | "Filter: search "acme"; status open; currency USD" | "All purchase orders".
- A note: "A read-only snapshot. Nothing was sent anywhere."

**Summary (one table in every format).** The columns are the screen's columns plus Currency, because totals in different currencies are never added together:

PO number · Vendor · Currency · Total · Balance · Status · Invoices · Entered

"Entered" uses the screen's wording: Form / Typed text / Uploaded document / Demo dataset.

**Detail, Financial:**
1. **PO**: number, vendor (and its status), currency, issued date, status.
2. **Totals**: total, committed, balance (marked "over-billed" when negative), awaiting review, consumed not assigned to a line.
3. **Invoices matched to this PO**: invoice number, file, total, decision at run time, status now, when, historic (yes/no).
4. **Lines**: line, description, quantity, unit price, amount, consumed quantity, consumed amount, remaining quantity, remaining amount.
5. **Ledger**: type, amount, invoice id, when.

**Detail, Full** adds:
6. **How the commits are allocated**: invoice, to (PO line N, or "PO total, no line"), amount, quantity, how (automatic / reviewer / legacy), when.
7. **Where this PO came from**: entered by, entered (when), document name, model and cost, fields the person changed, and the typed text (capped at 4,000 characters, with "(truncated)").

An empty table prints "None".

**Per format:**
- **PDF** (reportlab):
  - A4 landscape; a title, the header lines, then each section as a heading plus a table.
  - Table headers repeat on every page (`repeatRows=1`); long descriptions wrap; footer "Page N of M".
  - Money shown as `1,500.00` with the currency in the column header.
  - Base-14 Helvetica (decision 3).
- **Word** (python-docx):
  - A landscape section with the same order: Heading 1 for the title, a paragraph per header line, Heading 2 plus a table per section.
  - Table style "Light Grid Accent 1"; the header row repeats across pages; money right-aligned as `1,500.00`.
- **Excel** (openpyxl):
  - **Summary:** ONE sheet "Purchase orders". The header lines go in rows 1-5, the table header in row 7, panes are frozen below it, and an autofilter covers the table.
  - **Detail:** one sheet per section ("PO", "Totals", "Invoices", "Lines", "Ledger", and for Full "Allocations", "Provenance"); key/value sections are two columns; plus a first sheet "About" with the header lines.
  - Money cells are numbers written from `Decimal` (exact) with format `#,##0.00`; quantities are numbers; dates are text (ISO).
  - Text cells go through `safe_cell`. A text cell starting with `=` is never stored as a formula: the type is forced to string and the value prefixed with `'`.
- **CSV** (UTF-8 with BOM so Excel opens it correctly, CRLF, RFC 4180 quoting). Money is plain `1500.00` (no thousands separator, dot decimal) for machines.
  - **Summary:** the header lines as `Label,Value` rows, one blank row, then the table.
  - **Detail:** single file, stacked sections. Every section starts with a marker row `[Section name]`, then its header row, then rows, then one blank row:
```
Exported from,Invoice Agent
Generated (UTC),2026-10-02 14:15
What,Purchase order PO-SS-001: Full
Note,A read-only snapshot. Nothing was sent anywhere.

[PO]
Field,Value
PO number,PO-SS-001
Vendor,SuperStore (approved)
Currency,USD
...

[Totals]
Field,Value
Total,6000.00
Committed,5338.08
...

[Invoices]
Invoice number,File,Total,Decision at run time,Status now,When,Historic
10963,superstore_10963.pdf,5338.08,review,approved,2026-10-02T10:01:00Z,no

[Lines]
Line,Description,Quantity,Unit price,Amount,Consumed quantity,Consumed amount,Remaining quantity,Remaining amount
...

[Ledger]
Type,Amount,Invoice id,When
...

[How the commits are allocated]        (Full only)
...

[Where this PO came from]              (Full only)
Field,Value
...
```

**Formula-injection rule** (CSV and Excel text cells): a value starting with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading `'`.
- Money and quantity cells are numbers we produce from `Decimal`. They are written as numbers and never escaped, so a reversal stays `-1500.00`, not `'-1500.00`.
- Everything user-derived is escaped: PO numbers, vendor names, descriptions, file names, typed text.

## 3. Tick-boxes, filter and "nothing ticked"
- **Ticks track what is shown.** Each row has a tick-box, plus a header box for "select all shown" (checked / mixed / unchecked). The ticked set is held in the list screen as PO ids. When the search, status or currency filter changes, ticks on rows that are no longer shown are dropped, so a summary export can never contain a PO you cannot see.
- **The button says what it will export:**
  - with ticks: **Export 3 selected**, which sends `ids=…` plus the active filter (for the scope line);
  - without: **Export all 12 shown**, which sends only the active filter, so the server exports `po_list(q, status, currency)`, the same query the list ran.
- **Ticks apply ONLY to the summary export.** Row and detail exports ignore them and always export that one PO.
- **Cap (decision 4):** at most **1,000 POs** per summary export. Over the cap gives a 422: "1,250 purchase orders match; narrow the search or tick at most 1,000." `ids` also accepts at most 1,000.
- A detail table has no cap below the data's own limit (`po_max_lines` = 200 lines). Ledger and allocation sections are cut at 5,000 rows, with a "(truncated)" note, as a guard.

## 4. Test plan (offline; `pytest -W error` + vitest)
- **Numbers equal the screen.** On the demo database, after a real approval (reusing the review-action helpers, so there are commits, line consumption and allocations), each format is parsed back and every value compared with `po_detail` / `po_list` JSON:
  - CSV with the `csv` module;
  - XLSX with openpyxl (cell values are `Decimal`-equal);
  - DOCX with python-docx table cells;
  - PDF with pypdfium2 text extraction (already a dependency), checking each formatted amount and the PO number appear.
- **All four open:** pypdfium2 opens the PDF and counts pages; `docx.Document`, `openpyxl.load_workbook` and `csv.reader` load the others.
- **Levels:** Full has the allocations and provenance sections; Financial has neither.
- **Summary scope:**
  - `q`, `status` and `currency` give exactly the `po_list` rows;
  - `ids` gives only those POs, and an unknown id gives 404;
  - the scope line matches;
  - cap + 1 gives 422.
- **Injection:** a PO number `=HYPERLINK("x")`, a vendor `+cmd`, a line `@SUM(A1)` and a description `-2+3` are escaped in CSV and XLSX (string type, leading `'`, never a formula), while a reversal amount `-1500.00` stays a number.
- **File names:** a PO number of `../evil/<PO>.pdf` or with unicode gives a safe name; both `filename` and `filename*` are present.
- **Access gate:** with `ACCESS_TOKEN` set, both routes give 401 without the token and 200 with it.
- **No database writes:** row counts of every table, unchanged.
- **Empty and long:**
  - no POs: the summary has its header lines plus "None";
  - a PO with no lines, invoices or ledger: every section says "None";
  - a 200-line PO with 1,000-character descriptions: the PDF runs to several pages with repeated headers, the DOCX opens, the XLSX row count is right, and each finishes in under 5 s.
- **Frontend (vitest):**
  - "Export" with an icon, and no "Share" anywhere (a source scan);
  - the row and detail buttons are the same component and build the same request;
  - the request URLs carry the filter, the ids, the format and the level;
  - ticks drop when a row is filtered away;
  - "Export N selected" vs "Export all N shown";
  - the Blob download (`URL.createObjectURL` mocked) uses the server's file name;
  - a 401 opens the token dialog;
  - the menu works from the keyboard.

## 5. Build stages (commit after each; STATUS.md updated; test gate in brackets)
1. **E1: the model, CSV, Excel and the routes.**
   - Dependencies: reportlab moves to the main dependencies; python-docx is added.
   - Ships: `model.py`, `safety.py`, the CSV and XLSX renderers, both routes (PDF and DOCX answer 422 "not yet" until E2), SPEC §11 item 90.
   - [Number equality for CSV/XLSX, injection, file names, gate, no writes, scope, caps, empty / long.]
2. **E2: PDF and Word.**
   - Ships: `pdf_render.py`, `docx_render.py`.
   - [The four formats open; number equality for PDF/DOCX; the long PDF paginates; levels.]
3. **E3: frontend.**
   - Ships: `download.ts`, `POExportButton` (row + detail), the list tick-boxes and the summary export, regenerated fixtures.
   - [vitest, `tsc`, build.]
4. **E4: docs.** README section, STATUS, `EXPORT_REPORT.md`. Then stop for your browser check.

## 6. Open questions (with recommendations)
1. **Mixed currencies in the summary:** a Currency column and no cross-currency totals. **Recommend yes.** It matches the screen and never adds USD to INR.
2. **CSV header lines:** CSV is the one format where the scope cannot go in a title, so the "file states which filter applied" rule needs the `Label,Value` header block above the table (summary) and above the stacked sections (detail). **Recommend this.** The alternative is a pure table with the scope only in the file name.
3. **Non-Latin text in PDFs:** the built-in Helvetica covers Latin-1 only (no ₹, no CJK names). **Recommend v1 uses Helvetica and replaces characters it cannot draw with "?", noting it.** Word, Excel and CSV keep every character. The alternative is to bundle the free DejaVu Sans font (~700 KB file, no new package).
4. **Summary cap: 1,000 POs.** **Recommend yes.** Above that, the 422 asks you to narrow the filter.
5. **"Also considered in (not matched)"** is on the PO page but not in your Full list. **Recommend leaving it out:** it is matching diagnostics, not the PO's money or origin.
6. **Typed text in Full provenance:** include it, capped at 4,000 characters. **Recommend yes:** it is what the PO was drafted from, and it is on the page.
7. **`python-docx` pulls in `lxml`** (free, BSD) as a dependency of its own. **Recommend accepting it.** There is no Word writer without it except hand-written XML.
8. **One Export button with a small menu** (level + format) rather than eight separate buttons. **Recommend the menu.**
9. **SPEC:** no change to sections 4-6 (no schema or contract change), so nothing needs your approval; §11 gains item 90.
