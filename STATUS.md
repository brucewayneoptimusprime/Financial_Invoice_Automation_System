# STATUS

Last updated: 2026-09-25. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0, M1 (+ follow-ups 1-3) complete and approved.
- **M2 (ingest + extraction), approved plan: Stages 1-2 of 5 done** (LLM layer; ingest). Stage 3 (contract + extractor + CLI) is next; then I STOP for your live checks. Stages 4-5 wait for your word.
- No live API call has been made; nothing built so far needs a key.

## Test count and result
**787 passed, 0 failed, 2 deselected** (`pytest -W error`, ~18 s). +91 this stage. The 2 deselected are `@pytest.mark.live` (skipped cleanly without a key). Mutation-checked: trusting any file as a PDF fails 9 tests; classifying password-protected as system-side fails 2.

## What changed (this stage)
- New `app/ingest/`: `validate` (type by MAGIC BYTES, never the extension; empty/oversize/spoofed files rejected with clear codes, no run created), `store` (SHA-256, copy to `data/runs/<run_id>/original.<ext>` with a generated name; hostile names/run ids cannot escape), `render` (pypdfium2 + Pillow: page PNGs <=1568 px, EXIF fix, CMYK/palette/alpha -> RGB, JPEG fallback under the API's 5 MB image limit, decompression-bomb guard), `textlayer` (per-page text, usable/garbled/too-little assessment), `stage`.
- Failure kinds per your change: password-protected and blank -> `vendor_side`; corrupt/unrenderable -> `system_side`. They return a failed `IngestInfo` (run continues, $0 spent), not an exception.
- Page cap (default 10): extra pages are not processed and the stage is flagged `pages_truncated`.
- New system-side models `IngestInfo`, `ExtractionMeta` on `RunContext`; `meta.json` per run (no invoice text, no secrets).
- Test documents are generated at test time (reportlab/Pillow); no binaries committed.

## Decisions I need from the user
1. None blocking. After Stage 3 please run `python -m app.llm.probe` and then `python -m app.extraction.cli <file>`, and paste back the probe's result line.

## Assumptions added to SPEC section 11
None this stage (M2 assumptions are collected in Stage 5). Earlier: 8-20 (M0), 21-35 (M1), 36-37 (follow-ups).

## Known risks or gaps
- pdfium reports a zero-page PDF as a format error, so it is classified `corrupt_pdf` (system_side); the separate `no_pages` branch is defensive and untested against a real file.
- Blank-page detection is a pixel-variance threshold (stddev < 1.0); a near-blank page with faint content could be judged blank only if ALL pages are.
- Rendering quality on real scans/photos is untested (only generated documents so far).
- Some working-tree files have CRLF endings locally; git normalises to LF (cosmetic).
