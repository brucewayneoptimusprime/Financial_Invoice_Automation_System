# STATUS

Last updated: 2026-09-25. Repo: `C:\Zamp_ai_Automation` (git; spec is `SPEC.md`, plan is `PLAN.md`).

## Current milestone and state
- **M0** (foundations) and **M1** (rules engine, no LLM) are approved and complete.
- **M1 follow-ups** (reference floor, blocked-candidate ambiguity) are done and committed (`da9a25d`).
- **M2** (ingest + extraction): the plan is written to `PLAN.md` and **awaiting approval. No M2 code exists.**
- Working conventions adopted: this file is rewritten each stage/milestone; plans go to `PLAN.md`.

## Test count and result
**602 passed, 0 failed** (`pytest -W error`, about 12 s). That is +30 since the M1 approval (572): 22 reference-floor tests and 8 vendor-tie / blocked-candidate tests. The new guards were mutation-checked (disabling them fails 6 and 3 tests).

## What changed (this stage)
- New floor result `engine_floor_reference`: a PO reached through an inexact PO reference (contained or fuzzy) can never be approved; reason "reference X resembles PO Y". Exact normalised references (any formatting, explicit or inferred) are unaffected. Not a rule, cannot be disabled.
- `r_vendor_status`: ambiguity involving a blocked vendor stays severity 1 but sets `detail.blocked_candidate` and names the blocked vendor (`VendorMatch.candidate_vendor_ids`).
- `r_po_found` now says how the PO was actually found (it used to claim "explicit reference" even when the reference had not matched).
- A validate run now returns 12 rule results plus 2 floor results (14 total).
- Added `PLAN.md` (M2 plan) and this `STATUS.md`.

## Decisions I need from the user
1. **Stated PO reference that matches no PO** (new gap, see risks): recommend extending the reference floor so any stated reference that is not an exact match to the matched PO forces review.
2. **PDF library:** recommend pypdfium2 + Pillow (permissive) over PyMuPDF (AGPL).
3. **Contract additions** (`vendor_tax_id`, `vendor_address`, `document_type`, `adjustments`, `item_code`, reader-instruction flag, system fields `model_confidence`/`grounding`) plus new rule `r_document_type`: recommend approve all.
4. **Failure semantics:** our failures (timeout, schema, no key, cost ceiling) -> `review`; document failures (protected, corrupt, blank) -> `request_info`. Recommend approve; needs small M1 engine edits.
5. **Text + vision:** send both when a usable text layer exists, vision-only otherwise. Recommend approve.
6. **Over the page cap (default 10):** recommend process first N pages and force review, not reject.
7. **Currency `$`:** recommend map to USD by config (`€`->EUR, `£`->GBP; `¥` stays ambiguous).
8. **Ambiguous dates:** recommend best reading at confidence <= 0.5 plus a note (-> review), not null.
9. **Cost defaults:** recommend $0.25 per run, $5.00 per session, 4096 max output tokens.

## Assumptions added to SPEC section 11
36, 37 (this stage). Earlier: 8-20 (M0), 21-35 (M1).

## Known risks or gaps
- **Reference gap (decision 1):** an invoice quoting a PO number that matches no PO still approves if vendor, amount and lines match another PO. A test named `test_known_gap_...` documents current behaviour and must be changed with the decision.
- With no PO reference, amount fit alone cannot separate two otherwise identical same-vendor POs; the engine escalates to review rather than guess.
- The facts snapshot loads whole tables (fine for the demo, not for scale).
- No live LLM behaviour has been observed yet (no API key, no M2 code); M2 accuracy is unproven until the eval runs on real invoices.
- Cosmetic: some working-tree files have CRLF line endings; git normalises them to LF.
