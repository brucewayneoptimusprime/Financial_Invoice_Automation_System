# STATUS

Last updated: 2026-09-25. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0, M1 (+ follow-ups 1-3) complete and approved.
- **M2 (ingest + extraction): Stages 1-3 of 5 done** (LLM layer, ingest, contract + extractor + CLI). **I have STOPPED as instructed**: Stage 4 (grounding, injection scan, engine touch points) and Stage 5 (eval, manifest, live tests) wait for your word.
- Nothing has touched the live API. Everything is verified with mocks/recorded-style fixtures; the CLI and probe run for real but need your key.

## Test count and result
**1107 passed, 0 failed, 2 deselected** (`pytest -W error`, 30-70 s). +320 this stage. The 2 deselected are `@pytest.mark.live`. Mutation-checked: negating adjustment signs fails 7, skipping the null->0 rule fails 1, ignoring refusal/truncation fails 2, letting unreadable documents reach the model fails 4.

## What changed (this stage)
- **Contract** (SPEC 38-40): tax ID, address, document type, adjustments (sign applied by kind), item code, reader-instruction flag, system fields `model_confidence`/`grounding`; wire schema + drift test; prompt module with your additions (meaning not labels, total definition, Balance Due, Order ID is not a PO, total tax, ambiguous dates) and a fingerprint test that forces a version bump.
- **Parsers**: US / European / Indian digit grouping, brackets, `Rs.1,00,000/-`, dates, currency map incl. INR; tax-ID normalisation. Reused by grounding in Stage 4.
- **Extractor + stage**: path selection (text+vision / vision-only / text-only), one repair retry, degrade-to-all-null on every failure route (SPEC 41), artefacts in `data/runs/<id>/` (extracted.json, raw replies, meta.json).
- **CLI** `python -m app.extraction.cli <file>` prints extracted JSON, path used, tokens, cost, thinking/effort result, grounding status ("not run yet"). Exit codes 0/1/2/3. `--ingest-only`, `--replay`, `--record`, `--max-cost`, `--events`.
- SPEC 38-43 added.

## Decisions I need from the user
1. **Run the live checks** (Windows): put `ANTHROPIC_API_KEY=...` in `.env`; then `.venv\Scripts\python -m app.llm.probe` (about $0.0003) and `.venv\Scripts\python -m app.extraction.cli <invoice.pdf> --max-cost 0.10`. Please paste back the probe result line and the CLI summary block. Add `--record data\recordings` once to keep real responses for offline replay.
2. **Say "go" for Stage 4** when ready. Recommendation: go after reviewing one real extraction, since Stage 4 tunes grounding against real text layers.

## Assumptions added to SPEC section 11
38-43 (this stage). Earlier: 8-20 (M0), 21-35 (M1), 36-37 (follow-ups).

## Known risks or gaps
- **Until Stage 4:** a system-side extraction failure (e.g. timeout) still yields `request_info` (all fields null -> completeness rule) instead of `review`; invoices with a shipping/discount adjustment are flagged by `r_arithmetic` (adjustments not yet in its formula); tax ID is extracted but not yet used in vendor matching; `r_document_type` and grounding do not exist yet.
- Prompt accuracy and image-token cost are unmeasured until your live run; the `disabled` + `low` pair is unconfirmed (the client falls back automatically and says so).
- The test suite got slower (rendering); no single slow test.
- Some working-tree files have CRLF endings locally; git normalises to LF.
