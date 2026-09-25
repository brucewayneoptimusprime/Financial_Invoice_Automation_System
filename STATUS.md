# STATUS

Last updated: 2026-09-25. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0-M2 complete. **M3 (Pipeline): Stages 1-3 of 4 done** (seed + persistence; digest, templates, actions, runner; model explainer/drafter with claim checks). Stage 4 (CLI + six-invoice end-to-end) is next; I stop after it.
- No live call, no re-record. The explainer and drafter have never run against a real model (only a scripted double); their real quality is unmeasured.

## Test count and result
**1801 passed, 0 failed, 2 deselected** (`pytest -W error`). +78 this stage. 15 mutation checks (each claim check removed in turn, repair retry, draft payload leaking internal text, injection guard, cost accounting, internal-note-calls-a-model, document-text cleaning): all killed (one survivor found and closed with two new tests).

## What changed (Stage 3)
- **Explainer** (`explain-v1`): given ONLY the digest; accepted only if it passes `checks.py` (fact ids exist, every triggered fact cited, numbers/identifiers/quotes from the facts, real decision stated, no contradicting claim, sentence cap). **Drafter** (`draft-v1`): vendor emails only, given only vendor-safe request lines; checked for coverage of every request, invoice number, no invented numbers or references, no approval/payment promise, no internal wording, no contact details, signed "Accounts Payable", word cap.
- A bad reply is repaired once (correction lists our check problems only), then the deterministic template is used; the reason and both attempts' cost are recorded. A timeout, refusal, cost ceiling or missing key also falls back to the template, so the run never fails because of these roles.
- Reader instructions in a document keep both models away (templates only); document text placed in facts is cleaned and capped. Same client and cost ceilings as extraction; costs land in `runs`.
- The decision is fixed before either role runs; a model that claims "approved" on a review is rejected by the check, and a test proves the decision, invoice status and ledger are unaffected whatever comes back.
- SPEC section 11 items 66-67.

## Prompt/wire changes that need the end-of-M3 re-record
- **extract-v5 only** (currency named in words). The explainer/drafter prompts are separate and need no extraction re-record. Cost if nothing else lands: 6 calls, about $0.14 (will be restated before running).

## Decisions I need from the user
None.

## Assumptions added to SPEC section 11
66, 67 (earlier: 46-65).

## Known risks or gaps
- Real-model behaviour of the explainer/drafter (does it pass the checks, how often does it fall back to the template, cost per call about $0.006 estimated at 1.3k in / 380 out) is unknown until an approved live run; the checks are deliberately strict, so expect some template fallbacks at first.
- The claim checks catch invented numbers, identifiers, quotes, contradicting claims and internal wording; they cannot prove a sentence is faithful in meaning.
- Recordings are stale for replay until the end-of-M3 re-record.
