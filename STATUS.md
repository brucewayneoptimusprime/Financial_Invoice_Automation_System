# STATUS

Last updated: 2026-09-25. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0, M1 (+ follow-ups) complete. M2 Stages 1-3 done plus two live-check hotfixes. **Stopped as instructed: Stage 4 is NOT started.**
- **The second live failure ("compiled grammar too large") is fixed and proven live**: the new array-of-entries schema was ACCEPTED by the API on the first probe (1 of your 3 allowed attempts). `prompt_json` was not needed, so `json_schema` stays the default.
- **Disclosure:** I ran `python -m app.llm.probe --schema --all` believing no key existed; your `.env` was present, so it made 2 real calls (**about $0.017 total**). I never opened or printed `.env`; the key was read only through settings. Nothing else in this session has called the API (all tests blank the key).

## Test count and result
**1290 passed, 0 failed, 2 deselected** (`pytest -W error`, ~35 s). +95 this fix. Mutation-checked: dropping schema-error detection fails 7, prompt_json still sending a schema fails 2, truncation guard removed fails 3, duplicate names overwriting fails 3. No M1 engine or contract file changed.

## What changed (this fix)
- **Attempt A (built and passed live):** header fields = ONE array of entries `{name, found, value, page, source_text, confidence, flag}`; line items/adjustments stay small flat arrays. 5 objects / 29 properties / 1.9 KB (was 15 / 89 / 5.3 KB). Converter: missing names -> not found, duplicates keep the first + note, unknown names ignored + logged. Prompt `extract-v3` (only OUTPUT FORMAT + flag wording changed).
- **Fallback B (built, behind `LLM_STRUCTURED_OUTPUT=json_schema|prompt_json`):** prompt_json sends no schema, puts it in the prompt as text, parses tolerantly (fences, text around the JSON; truncated replies reported as invalid JSON), validates, same repair retry. The eval-script half of your item 5 does not exist yet (Stage 5); the reusable message helper does.
- **Pre-flight/clear failure:** the client maps schema/grammar 400s to `LLMSchemaError`; the extraction CLI prints a banner naming the switch and exits 4 instead of a "degraded" run.
- **Probe:** `--schema` (configured mode), `--structured MODE`, `--all` (both modes + a recommendation line).
- **Live evidence (exact):** `json_schema` ACCEPTED (in=1399 out=590, $0.008698, 8.3 s); `prompt_json` works (in=929 out=687, $0.008728); `thinking=disabled` + `effort=low` ACCEPTED, no fallback; the reply converted to an all-null invoice in both modes.

## Decisions I need from the user
1. **Now run one real invoice**: `.venv\Scripts\python -m app.extraction.cli <invoice.pdf> --max-cost 0.10` (add `--record data\recordings` to keep the real response), and paste the summary block. I did not run an invoice myself, per your instruction to iterate only with the probe.
2. **Say "go" for Stage 4** after that. Recommendation: go once one real extraction looks sane, since Stage 4 tunes grounding on real text layers.

## Assumptions added to SPEC section 11
45 (this fix; item 44 was the intermediate shape). Earlier: 8-20 (M0), 21-35 (M1), 36-37, 38-44 (M2).

## Known risks or gaps
- The probe proves the schema compiles and a blank document round-trips; it does not prove extraction QUALITY or that a full-size invoice reply stays under the 4096-token output cap (a long invoice is a truncation risk: it would degrade after one repair).
- Grammar-constrained output still cannot express "missing" as null; correctness relies on the model honouring found=false (the converter ignores placeholder content when found=false).
- Carried over: system-side extraction failures still yield `request_info` until Stage 4; adjustments not yet in the arithmetic rule; grounding, `r_document_type`, tax-ID vendor matching not built.
- Some working-tree files have CRLF endings locally; git normalises to LF.
