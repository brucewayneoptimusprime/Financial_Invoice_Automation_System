# Gmail import: stage report 3 (Plan 2: plain-English search and relevance labels)

Branch `feature/gmail-integration`. `master` is unchanged at `6efd185`. Nothing has been pushed. No secret, token or `.env` value appears here or in any commit. One paid live model call was made on purpose, for the measured cost below ($0.0072). Nothing else called Google or Anthropic.

Your answers to the plan's open questions were all "yes". For questions 4 and 5 I took "yes" to mean accepting my recommendation: ineligible attachments get no label, and an attachment the model skipped shows no label rather than "unsure".

## 1. Commits

| Stage | Commit | What |
|---|---|---|
| B1 | `40c953a` | **Query translator backend.** `prompts.py` (`gmail-query-v1`, `gmail-labels-v1`, pinned fingerprint) and `translate.py` (the allowlist plus the company-name rule, one repair retry). `POST /api/gmail/search {sentence}`. A failure is 422 `translation_failed` (`refused` or `unavailable`) with no Gmail call. Cost in the response; status flags; SPEC §7 row and §11 item 88 |
| B2 | `aa197a3` | **Sentence box in the panel.** "Describe what you're looking for" + Find. Claude's query goes into the editable manual box, with the notes and the cost line. Refused or unavailable translations keep the manual box working |
| C1 | `da38d24` | **Relevance labeller backend.** `labels.py` and `POST /api/gmail/labels`: metadata only, delimited, refs instead of Gmail ids, flagged emails labelled by rule, no call without eligible attachments, the 20% tolerance, one paid call per search. The advisory test; SPEC §11 item 89 |
| C2 | `c9d4601` | **Labels in the panel.** Chips with reasons next to the checkboxes, "Labelling…", the hints-only sentence, the combined cost line. The frontend advisory test; the one live test; README "Gmail import" section |

## 2. Test results (fresh run on the final code, `c9d4601`, 2026-10-02)

- **Backend:** `python -m pytest -W error` gave **2423 passed, 0 failed, 4 deselected**.
  - Before Plan 2: 2374. B1 added 30, C1 added 19.
  - The 4 deselected are the `live` tests: the two existing ones, the Gmail inbox test, and the new Gmail models test.
- **Frontend:**
  - `npx vitest run`: **117 passed** (before Plan 2: 101; B2 added 7, C2 added 9).
  - `npx tsc --noEmit`: clean.
  - `npx vite build`: OK.
- **Live:** `python -m pytest -m live -k gmail_models -s` gave **1 passed** (run once).

## 3. Existing test assertions changed

**None**, in either the backend or the frontend, in any of the four stages. Two related notes:
- **Fixtures regenerated.** The `gmail_*.json` frontend fixtures were regenerated in B1, B2 and C2, because status, search and labels gained fields; the shape-drift test catches it every time. Five fixtures are new: `gmail_status_live`, `gmail_search_sentence`, `gmail_translate_422`, `gmail_labels` and `gmail_labels_skipped`.
- **Cost-line wording kept.** In C2 my first version of the combined cost line changed the B2 wording ("… (query by Claude)") and failed one B2 test. **I changed the component, not the test.** A query-only search keeps the B2 wording, and the combined form appears only when labels add to it.

## 4. Measured tokens and cost per search (live test, claude-sonnet-5 at $2 / $10 per million tokens)

- **Setup:** the sentence was "invoices from SuperStore since September", run against the labelled fake inbox's metadata (real model, no Gmail).
- **Claude's query:** `SuperStore invoice after:2026/09/01`. The company name stayed a plain keyword, and it passed the allowlist.
- **What was labelled:** 5 emails; 4 attachments were sent to the model. The 5th is in the flagged email and got `unsure` by rule.

| Call | Input tokens | Output tokens | Cost |
|---|---|---|---|
| translate | 778 | 59 | $0.002146 |
| labels (4 attachments) | 1,486 | 208 | $0.005052 |
| **this search** | | | **$0.007198** |

The labels came back sensible: the four SuperStore invoice PDFs are `likely_invoice`, with "Filename and subject explicitly say invoice from SuperStore billing".

**Extrapolation (not measured):**
- Per attachment, the labeller used about 220 input and 52 output tokens. The plan estimated about 110 and 35; the delimiters and JSON cost more than estimated.
- A full page of 25 emails with about 35 importable attachments is therefore about 8,300 input and 1,800 output tokens, roughly **$0.035 for labels and about $0.037 per search** (the plan estimated $0.025).
- The worst case, at the 60-attachment cap with a repair retry, is about $0.12.

**Budget:** both calls use the search's run key and the server's metered client.
- One search stays well under the $0.25 per-run ceiling.
- The $5.00 per-session ceiling (local) allows about 135 full searches.
- When the ceiling is reached, the panel falls back to the manual box and no labels.
- The import pre-check counts what searches have spent.

## 5. Deviations from GMAIL_PLAN_2

1. **One labels call per search, cached.** A repeat labels request for the same search is answered from a per-search cache, free, and the cache is dropped with the search session. The plan said labels are never stored in the session; they are not stored in the session object or the database. The cache only stops a double request from paying twice.
2. **Rule labels on fallback.** Flagged emails get their `unsure` rule label when labelling runs, but on a fallback (`invalid_output` / `unavailable`) **all** labels are dropped, so "no labels" really means none.
3. **Date and timezone in the translator payload.** The translator gets today's UTC date and "UTC", not the server's local timezone. The rest of the query code uses UTC dates too.
4. **The labels payload carries `intent_kind`** (`sentence` | `query`) next to the intent, so the prompt knows which one it got.
5. **Costs are 6-decimal strings** (`"0.002146"`) in every response; the UI rounds them.
6. **The refresh after an import** re-runs the exact query that was sent (`query_sent`) and keeps the labels already shown. It makes no translation or labels call, so an import never costs model money.

## 6. Unresolved items and things to watch

- **Replay recordings are date-bound for sentence searches.** The translator's payload includes today's date, and the replay key hashes the whole request. A recorded sentence therefore replays only on the day it was recorded.
  - Labels replay only if the inbox returns exactly the same emails, because their metadata is in the payload.
  - On any other day or inbox state, replay falls back to the manual box and no labels. That is safe, but it matters for your demo recording (section 7b).
  - A later fix could leave the date out of the replay key.
- **Claude may add the word "invoice"** when you say "invoices" (it did: `SuperStore invoice ...`). That narrows the search to emails containing the word. The prompt allows it; edit the query if it hides something you expect.
- **Each new search pays for labels again,** including "Search again" with an edited query. The refresh after an import does not.
- **Label quality on your real inbox is untested;** the live test used the fake inbox's metadata.
- **Still open from earlier:** SPEC item 86 (the dedupe row of a run rejected after import); no "From Gmail" marker on the dashboard; `messages.get` without a `fields` mask.

## 7. Steps for you

### (a) Restart the backend and try the sentence box and labels against your real inbox

The new features need a model, so start in `--live` mode. Replay has no recordings for the new prompts yet, so on replay they fall back.

**Terminal 1:**
```powershell
cd C:\Zamp_ai_Automation; .\.venv\Scripts\Activate.ps1; cd backend
Remove-Item Env:GMAIL_BACKEND -ErrorAction SilentlyContinue
python -m app.api.serve --live
```
- Expect the `LIVE MODE` banner, then `Gmail import: real Gmail, read-only. OAuth callback: http://localhost:8000/...`.
- Plan 2 added no new dependency or database change.
- Each search costs about $0.01-0.04 (shown in the panel). Each imported invoice's extraction costs about $0.03, as before.

**Terminal 2:**
```powershell
cd C:\Zamp_ai_Automation\frontend; npm run dev
```

**Browser:** open **http://localhost:5173/invoices**.
1. Your inbox should still be connected; the stored token survives restarts. If the panel says "connect again" (for example, after the 7-day Testing-mode expiry), click **Connect Gmail (read-only)** and approve as before.
2. In **Describe what you're looking for**, type a sentence about mail you know is there, e.g. `invoices from <a company that emailed the inbox> since September`, and click **Find**. Check:
   - the **Gmail search** box now holds Claude's query, with the company name as a plain word (not `from:Company`);
   - "Claude wrote this search…" shows the notes;
   - "Sent to Gmail" shows the query plus `has:attachment`.
3. Within a few seconds, "Labelling the attachments…" turns into chips (**likely invoice** / **unsure** / **unlikely**) with reasons next to the checkboxes. Check:
   - **nothing is ticked**;
   - the order is Gmail's (newest first);
   - greyed (ineligible) files have no chip.
4. Check the cost line: "This search: $… (query) + $… (labels) = $…, by Claude."
5. Edit the query in the Gmail search box and press **Search again**. The typed search costs nothing to translate, and labels run again for the new results.
6. Try a sentence Gmail cannot express, e.g. `invoices over 5000 dollars`. Claude should say why, and the manual box keeps working.
7. Tick one or two attachments (whatever their labels say) and click **Import**. They appear under **This upload** and run as usual.

### (b) Record one demo session for free replays later

```powershell
cd C:\Zamp_ai_Automation; .\.venv\Scripts\Activate.ps1; cd backend
python -m app.api.serve --live --record ..\data\recordings
```
(`..\data\recordings` is `data\recordings` at the repository root, the folder replay already uses.)

1. In the browser, do exactly the demo you want to show later: the sentence search(es), the labels, an import or two. Every model call (translations, labels, extractions) is saved as one JSON file in `data\recordings`. The folder is gitignored; keep it local, because label reasons may quote your emails' subjects.
2. Stop the server (Ctrl+C) and replay:
   ```powershell
   python -m app.api.serve --replay ..\data\recordings
   ```
   The same steps then cost $0.
   - **Gmail itself is always live**, even on replay: only the model calls are recorded.
   - **Record on the day you present.** A sentence search replays only on the date it was recorded, and labels only while the inbox returns the same emails (section 6). Otherwise the panel falls back to the manual box and no labels.
   - Typed (manual) searches never need a recording.
