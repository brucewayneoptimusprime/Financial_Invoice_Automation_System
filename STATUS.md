# STATUS

Last updated: 2026-09-25. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0, M1 (+ follow-ups) complete. M2 Stages 1-3 done; **hotfix for the live-check failure done** (wire schema redesigned). **Stopped as instructed: Stage 4 is NOT started.**
- Live check result you reported: the API rejected the wire schema (HTTP 400, 49 union-typed parameters, limit 16). Nothing was spent; the degrade path worked.
- The fix is verified offline only. **Whether the API now accepts the schema is unproven until you run `python -m app.llm.probe --schema`.**

## Test count and result
**1195 passed, 0 failed, 2 deselected** (`pytest -W error`, ~30 s). +88 this hotfix. Mutation-checked: reintroducing a nullable field fails 5 tests, ignoring found=false fails 3, making one property optional fails 4. No M1 engine or contract file changed.

## What changed (this fix)
- **Wire schema (only what the API sees) has ZERO unions, nulls and optional properties**: each field is `{found, value, page, source_text, confidence}` with placeholders when absent; nullable booleans are enums yes/no/unknown; line items, adjustments, notes use ""/0. Now 15 objects, 89 properties, 5.3 KB (SPEC 44).
- **`from_wire` converts back** to the unchanged nullable contract (found=false -> null/None/0; found=true with empty value -> not found + system note). Bad structure -> the single repair retry. Prompt is `extract-v2` ("if a field is not present, set found=false and leave placeholders; do not guess").
- **API limits, read again from the docs:** the documentation lists only unsupported keywords (no recursion, no numeric/string constraints, minItems 0/1, additionalProperties:false) and states **no numeric limits**; the only hard number known is the API's own "16 unions". `test_wire_limits.py` enforces 0 unions/nulls/optionals plus my own conservative budgets for unpublished limits.
- **Probe:** `python -m app.llm.probe --schema` sends the REAL wire schema with a text-only prompt (about a cent or less, capped at 1500 output tokens) and reports accepted/rejected, tokens, cost, and whether the reply converts to an all-null invoice.
- Fixtures regenerated in wire format; SPEC item 44; PLAN 5.3 annotated.

## Decisions I need from the user
1. **Run `.venv\Scripts\python -m app.llm.probe --schema` and paste the output.** If the API rejects something else (e.g. total property count), the message will say what; I will trim the schema (recommended fallback: split extraction into two calls, header fields then line items).
2. Then the earlier live checks: `.venv\Scripts\python -m app.llm.probe` and the CLI on one invoice. Say "go" for Stage 4 after one clean real extraction.

## Assumptions added to SPEC section 11
44 (this fix). Earlier: 8-20 (M0), 21-35 (M1), 36-37, 38-43 (M2 stages 1-3).

## Known risks or gaps
- 89 properties / 15 objects is my judgement of "small"; the API's real ceiling for those is unpublished, so the probe is the only authority.
- Structured output still cannot express "a missing value" as null; correctness now rests on the model honouring found=false (the converter ignores placeholder content when found=false, and the prompt forbids guessing).
- Carried over: system-side extraction failures still yield `request_info` until Stage 4; adjustments not yet in the arithmetic rule; grounding, `r_document_type`, tax-ID vendor matching not built.
- Some working-tree files have CRLF endings locally; git normalises to LF.
