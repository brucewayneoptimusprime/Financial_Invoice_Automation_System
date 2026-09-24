# STATUS

Last updated: 2026-09-25. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0 and M1 complete and approved. M1 follow-up 3 done (reference floor now covers any stated PO reference that is not an exact match, including one that matches no PO).
- **M2 (ingest + extraction), approved plan, Stage 1 of 5 done: the LLM layer.** Stages 2 and 3 are next; then I STOP for your live checks. Stages 4-5 wait for your word.
- No live API call has been made; no key is needed for anything built so far.

## Test count and result
**696 passed, 0 failed, 2 deselected** (`pytest -W error`, ~14 s). The 2 deselected are `@pytest.mark.live` tests; `pytest -m live` skips them cleanly when no key is set (verified). +88 tests this stage. Mutation-checked: removing key scrubbing fails 1 test; disabling the cost ceilings fails 7.

## What changed (this stage)
- New `app/llm/`: typed errors, price table + exact `Decimal` cost, cost ceilings (per run $0.25 / per session $5.00, reserve-then-settle, thread-safe), `AnthropicClient` (SDK does retries/backoff; we send `thinking=disabled` + `effort=low` + JSON-schema output), `MeteredClient`, record/replay fixtures, injectable `LLMClient` protocol.
- **Thinking/effort fallback built in:** if the API rejects `thinking=disabled` with `effort`, the client retries once with thinking omitted (effort=low kept), remembers it, logs a warning and reports it. `python -m app.llm.probe` (or `llm-probe`) makes one tiny call (~$0.0003) and prints ACCEPTED or REJECTED + fallback.
- Missing/blank `ANTHROPIC_API_KEY` -> clear `LLMConfigError` message (no crash). An autouse fixture blanks the key for every non-live test, so a real key in `.env` can never be used by a mocked test. Key is scrubbed from all messages/logs/`repr`/fixtures (canary tests). I have not read `.env`.
- Config additions (LLM, prices, ceilings, ingest, extraction, currency map incl. INR); `data/runs/` and `data/recordings/` gitignored; deps added (anthropic 1.8, pypdfium2, Pillow, reportlab dev).
- Reference floor extended (M1 follow-up 3, commit `3be510e`); PLAN.md records your approved changes.

## Decisions I need from the user
1. None blocking. After Stage 3 you run the live checks (`python -m app.llm.probe`, then `python -m app.extraction.cli <file>`); please paste the probe's first line back so I know which thinking mode applies.

## Assumptions added to SPEC section 11
36 (extended). Stage 1 adds none. Earlier: 8-20 (M0), 21-35 (M1), 37 (M1 follow-up 2).

## Known risks or gaps
- Prompt quality and image-token cost are unmeasured until the first live run; the mocked suite proves plumbing, not accuracy.
- The `disabled` + `low` combination is documented as valid for Sonnet 5 but unconfirmed; the probe settles it.
- Pre-call cost projection uses a pessimistic chars/3 token estimate and w*h/750 for images; real usage is what is billed and recorded.
- Some working-tree files have CRLF endings locally; git normalises to LF (cosmetic).
