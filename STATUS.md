# STATUS

Last updated: 2026-09-25. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0-M2 complete. **M3 (Pipeline): all 4 stages built and committed. Stopped as instructed. Nothing re-recorded, no live call this milestone.**
- New: `python -m app.pipeline.cli <file> (--replay DIR | --live [--record DIR]) [--db PATH] [--reset-demo] [--json]` runs the whole pipeline and prints ingest, extraction, match, every rule, decision, explanation, drafts, database writes and cost. It refuses to call the API without `--live` (so does the M2 extraction CLI now: decision 8).

## Test count and result
**1845 passed, 0 failed, 2 deselected** (`pytest -W error`). Stage 4 added 42. Mutation checks on the CLI guards and the six-invoice test: killed (one equivalent mutant: `allow_live=True` is unreachable behind the earlier guard).

## What changed (Stage 4)
- Pipeline CLI (above); extraction CLI `--live` guard; fixtures for all six samples (5 SuperStore replies recorded live with extract-v4, IQ v4 reply, three more PDFs).
- Wording polish found while reading real output: digest facts read `r_po_found (Invoice matches a purchase order) - matched_without_reference, severity 1: ...`; the review reason no longer repeats itself; "a invoice" -> "an invoice".
- SPEC section 11 item 68.

## The six real invoices, end to end (demo dataset; models = scripted double, extraction = replies recorded live)
| Invoice | Decision | Why (one line) |
|---|---|---|
| SuperStore 10963 | **review** | PO-SS-001 is a confident match, but the invoice prints no PO number (`r_po_found` matched_without_reference); every other check passes |
| SuperStore 24429 | **review** | same: PO-SS-002 matched, no PO number printed |
| SuperStore 14021 | **review** | same: PO-SS-003 matched, no PO number printed |
| SuperStore 6459 | **review** | same: PO-SS-004 matched, no PO number printed |
| SuperStore 14130 | **review** | same: PO-SS-005 matched (partly consumed, 7,500 left), no PO number printed |
| IQ Electronics scan | **request_info** | the v4 call returned no currency (required field), no PO number, no confident PO match (amount not comparable without a currency), invoice date only 0.50 (05-01-2025 is ambiguous); vendor found by GSTIN, tax null/included, arithmetic passes |

The five reviews sit in the queue, no money moved; the IQ run saved a draft vendor email (status draft, `to` NULL) asking for the currency, the invoice date and the PO number, with nothing internal in it.
**Clean invoice flagged?** Yes, by design: none of the five SuperStore invoices prints a PO number, so none can be approved (your rule: no reference + confident match = review). **The approve path is shown only by a clearly labelled SYNTHETIC controlled variant** (real invoice 24429 with `PO-SS-002` edited into the recorded reply): approve, ledger commit 1,770.61, PO-SS-002 balance 2,500.00 -> 729.39, status partially_billed. It is named `controlled_variant_synthetic_*` in tests, tagged `[SYNTHETIC CONTROLLED VARIANT ...]` in the reply's notes (visible in the CLI's `--json`), and never counted among the six.

## Prompt/wire changes that landed (for the end-of-M3 re-record; NOT run)
- **extract-v5 only** (a currency named in words returns its ISO code). No other extraction prompt or wire change landed in M3 (the explainer `explain-v1` and drafter `draft-v1` prompts are separate and need no extraction re-record).
- **Re-record cost: 6 live calls, about $0.14** (the v4 pass cost $0.140456; v5 adds roughly 30 prompt tokens per call, so expect about $0.141). It would also re-verify the IQ draft (currency INR expected), the five SuperStore entries, and restore `--replay` for the eval and this CLI. I will not run it until you confirm.

## Decisions I need from the user
1. **Confirm the re-record** (6 calls, about $0.14) - and then verify the IQ manifest entry (`currency` should be `"INR"`, not null).
2. **Optional:** a live run of the explainer/drafter to see how the real model does against the claim checks (about $0.006 per call; the whole six-invoice set through the CLI would be about $0.21). Recommendation: fold it into the same session as the re-record.

## Assumptions added to SPEC section 11
61-68 (M3). Earlier: 46-60.

## Known risks or gaps
- The real explainer/drafter have never run; the double is grounded in the digest, so the six results above show the plumbing and the checks, not model quality. The checks are strict, so expect some template fallbacks at first.
- `to` is always NULL (no vendor contact data); a blocked-vendor reject writes an internal notification instead of an email.
- With no PO number printed, SuperStore-style invoices can never auto-approve (your rule). PO matching there rests on vendor + amount + line text: scores were 0.57-0.60 against a 0.50 minimum.
- Recordings in `data\recordings` are extract-v4; `--replay` on the CLIs and eval misses until the re-record.
- Extraction varies between calls (IQ currency/subtotal changed between v3 and v4); one verified pass per file is not a stability measurement.
