# STATUS

Last updated: 2026-09-25. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0-M2 complete. **M3 (Pipeline): Stage 1 of 4 done** (seed + persistence layer). Stages 2-4 follow; I stop after Stage 4.
- No live call, no re-record. Recordings in `data\recordings` were made with extract-v4 and will not replay against extract-v5 until the single end-of-M3 re-record.

## Test count and result
**1675 passed, 0 failed, 2 deselected** (`pytest -W error`). +35 this stage.

## What changed (Stage 1)
- **Demo seed:** `data\seed_demo.json` (`_DATASET`, clearly not the placeholder): SuperStore, Electronics Mart India Ltd (IQ, GSTIN 36AAFCE1683D1ZT), PO-SS-001..005 (one per SuperStore invoice, full printed line text), PO-IQ-2025-001 (INR, full product text), one partly consumed PO (PO-SS-005: 9,000 - 1,500 = 7,500 derived). `data\seed.json` untouched. `SeedFile` needs exactly one of `_PLACEHOLDER` / `_DATASET`; `python -m app.db.reset --demo` loads it. A test checks every SuperStore PO line equals the verified manifest's printed line.
- **Persistence layer** `app/pipeline/persist.py` (all M3 SQL; no commits inside functions, caller owns the transaction): runs (start, finish-once), `AuditWriter` (monotonic seq, JSON-safe detail), `save_invoice` (+ lines, integer cents, NULL + note for missing or sub-cent, full extraction JSON kept), `transaction()` (BEGIN [IMMEDIATE] / rollback).
- **Prompt extract-v5** (decision A): currency named in words returns its ISO code; clause tests and fingerprint updated. **Not re-recorded** (bundled into the single end-of-M3 pass, as you said).
- SPEC section 11 items 61-63.

## Decisions I need from the user
None for Stage 1. Prompt/wire changes that will need the re-record so far: **extract-v5 only** (currency in words). I will list any others when Stage 4 ends; the re-record cost will be stated before running it (currently 6 calls, about $0.14).

## Assumptions added to SPEC section 11
61, 62, 63 (earlier: 46-60).

## Known risks or gaps
- Recordings are stale for replay until the re-record (extract-v4 keys vs extract-v5 requests). M3 tests use the committed reply fixtures, not the recordings.
- Extraction nondeterminism: the IQ scan's currency and subtotal changed between the v3 and v4 calls.
- The IQ scan still has no verified manifest entry.
