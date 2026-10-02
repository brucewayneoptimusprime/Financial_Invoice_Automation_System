# PO export: build report (E1-E4)

Branch `feature/po-export`, created from `feature/gmail-integration` at `81cab06`. `master` (`6efd185`) and `feature/gmail-integration` (`81cab06`) are unchanged, and nothing has been pushed. No model call, no Google call and no secret appears anywhere: the export is read-only and costs nothing.

## 1. Commits

| Stage | Commit | What |
|---|---|---|
| plan | `65bbe18` | `EXPORT_PLAN.md` |
| E1 | `a1f0bbd` | **Model, CSV, Excel, routes.** `app/po/export/`: the document model from `po_list` / `po_detail` (exact `Decimal`), safety (formula-injection escaping, file names, caps), the CSV and Excel renderers, the format registry. `GET /api/pos/export` (summary) and `GET /api/pos/{id}/export` (one PO). reportlab moved to the main dependencies, python-docx added. SPEC §11 item 90 |
| E2 | `c302bef` | **PDF and Word.** The PDF renderer (A4 landscape, repeated headers, "Page N of M", Helvetica with "?" plus a footer note) and the Word renderer (landscape, headings, repeating header rows). The full entry × level × format matrix |
| E3 | `bb590a6` | **Frontend.** `download.ts` (fetch with the token, Blob, the server's file name), `ExportMenu` (download icon, level + format menu), `POExportButton` (one component for the row and the PO page), the list tick-boxes and summary export. CORS exposes `Content-Disposition` |
| E4 | `d3d82d4` | **Docs.** README "Export purchase orders" section and known limitation; SPEC item 90 final; STATUS |

**Packages added** by `pip install -e ".[dev]"` (run in `.venv`, compared with `pip freeze` before and after):
- **python-docx 1.2.0**;
- **lxml 6.1.3**, which python-docx requires.

`reportlab` 5.0.1 was already installed as a dev extra; it moved to the main dependencies, so Render's `pip install -e .` now installs it. `openpyxl` was already a main dependency. Nothing else was added.

## 2. Test results (fresh run on `d3d82d4`, 2026-10-03)

- **Backend:** `python -m pytest -W error` gave **2476 passed, 0 failed, 4 deselected**. The 4 are the `live` tests.
  - New tests: 53. E1 added 30, E2 added 22, E3 added 1 (the CORS header).
- **Frontend:**
  - `npx vitest run`: **136 passed** in 14 files (14 new in `src/test/poExport.test.tsx`).
  - `npx tsc --noEmit`: clean.
  - `npx vite build`: OK.
- **Real-server smoke:** `serve --offline` on a scratch database. All eight requests answered 200 with the right `Content-Type` and file name:
  - summary CSV and XLSX (with a status filter);
  - summary PDF (ticked ids) and DOCX;
  - PO-SS-005 as Full PDF, Financial DOCX, Full XLSX and Full CSV.

  Each file started with its signature: BOM, `PK`, `%PDF`.

## 3. Existing test assertions changed

**None**, in either the backend or the frontend. New columns in the PO list (tick-box, export) did not break any existing list test.

## 4. What was built and tested: entry / level × format

Every cell below is built, answers 200 with the right media type and an attachment file name of the right extension, opens in its reader, and writes nothing to the database. "Values" means each number was compared with the screens' own JSON (`GET /api/pos`, `GET /api/pos/{id}`).

| Entry / level | PDF | Word (.docx) | Excel (.xlsx) | CSV |
|---|---|---|---|---|
| **Summary** (list: all shown, filter, ticked ids) | ✓ every PO number, total, balance and the scope line found in the text | ✓ one table; numbers and order equal the list | ✓ one sheet; numeric money (`#,##0.00`), frozen header, autofilter, scope cell; filter applied | ✓ header block + table; every cell equals the list; filters, ticked ids, scope, cap, 404 / 422 |
| **Financial** (one PO, row or page) | ✓ totals, lines, ledger values in the text; no metadata sections | ✓ (opens; same model as Full) | ✓ sheets About, PO, Totals, Invoices, Lines, Ledger with equal values | ✓ stacked `[PO] [Totals] [Invoices…] [Lines] [Ledger]`; every value equal |
| **Full (with metadata)** (one PO) | ✓ all of the above plus allocation amounts, "Page 1 of N", currency headers | ✓ headings in order; totals, lines and ledger equal; header rows repeat (`w:tblHeader`) | ✓ also Allocations and Provenance sheets; allocation amounts equal | ✓ also `[How the commits are allocated]` and `[Where this PO came from]` |

**Cross-cutting tests:**
- **Formula injection:** a PO number `=HYPERLINK(...)`, a vendor `+cmd…`, and lines `@SUM(…)`, `-2+3` and a tab-led text all get a leading `'` in CSV and Excel. No Excel cell is a formula, and a reversal stays the number `-3.00`.
- **File names:** `../evil/<PO> № 7.pdf` becomes a safe name, with `filename*` present.
- **Access:** 401 without `ACCESS_TOKEN`, 200 with it.
- **Empty and long:**
  - an empty database and an empty PO print "None";
  - a 200-line PO with about 1,000-character descriptions exports in CSV / Excel in under 5 s;
  - the same PO's PDF runs to more than 10 pages with the Lines header on the following pages and "of N" on every page, and its Word table has 201 rows.
- **The PDF font:** an unusual-character PO gives "?" plus the footer note in the PDF, while Word keeps "₹ 漢字 ✓". A normal PDF has no note.
- **Frontend:**
  - "Export" with an icon, and no "Share" anywhere in the UI source;
  - the row and the PO page build the identical request;
  - ticks never change a one-PO export;
  - **clicking a row's Export button does not navigate**, and **clicking a row's tick-box does not navigate** (your two tests);
  - "Export all N shown" carries the filter and "Export N selected" the ids, in list order;
  - select-all, including the mixed state;
  - a tick on a filtered-away row is dropped;
  - Escape closes the menu and returns focus;
  - a 401 shows the message, asks for the token and saves nothing;
  - the file name is parsed from `Content-Disposition`.

## 5. Deviations from EXPORT_PLAN

1. **"Entered" in the summary** uses the **list screen's** wording ("Manual", "Text", "Document", "Seed"), not the PO page's longer wording that the plan quoted. This keeps the summary equal to the list. The Full export's "Where this PO came from" uses the PO page's wording ("Form", "Demo dataset", …).
2. **Unit price has its own column kind** (`price`): a decimal shown as stored, with the currency in its header, like money in Word and PDF. The document model also carries the PO's currency.
3. **The summary header block has 6 lines** (plus "Purchase orders: N"). The Excel table therefore starts at row 8, not row 7.
4. **New: CORS exposes `Content-Disposition`.** Without it, a deployed frontend on another domain could not read the server's file name and would fall back to a generic one.
5. **Excel escaping shows the apostrophe.** An escaped Excel text cell visibly shows its leading `'` (e.g. `'=HYPERLINK(...)`): it is stored as text, not as Excel's hidden quote prefix. This matches the CSV and the plan's rule.
6. **PDF text collapses repeated spaces,** as reportlab paragraphs do. Word, Excel and CSV keep them.

## 6. Unresolved items

- **I could not look at the screens or files in an app.** The Chrome extension was not connected. I checked PDFs by rendering pages to images; Word and Excel I checked only by reading them back. Opening them in Word, Excel and a PDF viewer is yours (section 7).
- **PDF characters:** characters outside Latin-1 become "?" (your v1 decision); README lists it as a known limitation.
- **The 5,000-row guard** on ledger and allocation sections is in place but was not exercised at that size. The 1,000-PO summary cap was tested by lowering the cap.
- **Timestamps** in files and file names are UTC, so a file exported at 00:30 IST carries the previous day's UTC date.
- **Deployment:** cross-origin download (Vercel → Render) is covered by the CORS header test but has not been tried on the real domains.
- `PROJECT_STATE.md` is still untracked, as before.

## 7. How to check it in the browser

**Terminal 1 (backend):**
```powershell
cd C:\Zamp_ai_Automation; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"              # already done in this .venv; harmless to repeat (python-docx, lxml)
cd backend
python -m app.api.serve --offline     # or: --replay ..\data\recordings   (export needs no model either way)
```
Your own `data\app.db` is used as it is, with no reset. Add `--reset-demo` only if you want the demo POs instead.

**Terminal 2 (frontend):**
```powershell
cd C:\Zamp_ai_Automation\frontend; npm run dev
```

**Browser:** open **http://localhost:5173/pos**. Files land in your browser's Downloads folder.

1. **Summary, everything shown.** With nothing ticked, the button above the table reads **Export all N shown**. Click it, then choose **PDF**, **Word (.docx)**, **Excel (.xlsx)** and **CSV** in turn.
2. **Summary, filtered.** Pick a status in the filter (e.g. Open). The button count changes; export again. The file's "Scope" line says `Filter: status open`.
3. **Summary, ticked.** Tick two rows. The button reads **Export 2 selected**; export. Only those two POs are in the file, and the scope says `Ticked: 2 of N shown`. Check that ticking a box **does not** open the PO page.
4. **One PO from a row.** Click the small **Export** at the far right of a row (it must **not** open the PO page). Choose **Full (with metadata)**, then **Excel (.xlsx)**. Repeat with **Financial** → **PDF**.
5. **One PO from its page.** Open a PO (click its number). Use **Export** next to the title; it is the same menu with the same files.
6. **Compare** a few numbers in each file with the screen: Total / Committed / Balance / Awaiting review / Consumed not assigned, the line amounts, the ledger. They must match exactly.

**What to open each file with:**

| File | Open with | What to look at |
|---|---|---|
| `.pdf` | Edge / Chrome (drag the file in) or Acrobat Reader | Landscape pages; title and header lines; section headings; table headers repeat on every page; footer "Page N of M"; amounts as `1,500.00` with `(USD)` in the headers |
| `.docx` | Microsoft Word (or LibreOffice Writer) | Heading per section; tables in the "Light Grid" style; on a long PO the table header row repeats at the top of each page |
| `.xlsx` | Microsoft Excel (or LibreOffice Calc) | Summary: one sheet with the scope in the first rows, a frozen header and a filter. One PO: sheets About, PO, Totals, Invoices, Lines, Ledger (+ Allocations, Provenance for Full). Money cells are **numbers** (SUM works on them), and no cell contains a formula |
| `.csv` | Excel (double-click; the BOM makes accents show correctly) or any text editor | Summary: `Label,Value` lines, a blank line, then the table. One PO: header lines, then `[PO]`, `[Totals]`, `[Invoices matched to this PO]`, `[Lines]`, `[Ledger]` (+ the two Full sections), each followed by its rows |

**Optional injection check:** create a PO on the **New purchase order** form whose PO number starts with `=` (e.g. `=1+1`), if the form accepts it, then export it as CSV and Excel. The cell shows `'=1+1` as text, never a calculated `2`.
