# STATUS

Last updated: 2026-09-25. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0-M3 complete; end-of-M3 re-record with extract-v5 done ($0.143; IQ currency now INR).
- **M4 (API + live run view): stages 1-4 of 5 done** (runner timing events; the HTTP API; the SSE stream; the React frontend).
- Next: stage 5 (browser check of the six invoices on replay, screenshots here), then stop.
- No live calls in M4 (owner runs `--live` after seeing the UI on replay).

## Test count and result
- Backend: **1911 passed, 0 failed, 2 deselected** (`pytest -W error`); stage 2 added 44, stage 3 added 13 (`tests/api/test_sse.py`, repeated 5 times: stable).
- Frontend: **25 passed** (`npm test`, vitest); `tsc --noEmit` clean; `vite build` OK (256 kB JS, 79 kB gzipped).

## What changed (M4 stage 4)
- `frontend/`: React 19 + Vite 8 + TypeScript 5.9, no UI or state library. `npm run dev` serves http://localhost:5173 and proxies `/api` to the API on 127.0.0.1:8000.
- Screens: upload (drop zone, mode badge LIVE / REPLAY / OFFLINE, spend, recent runs) and the run page (`/runs/:id`): a 7-stage timeline driven by the SSE stream (waiting -> running with elapsed time -> done / flagged / failed, duration, one-line summary, expandable events as key/value rows, rule results as they arrive), which becomes the result view when the stream ends: decision banner (word + icon + colour), the explanation and its source, extracted fields (value, effective and model confidence, evidence status, page, source text, click to open the page image), checks (triggered first, expandable numbers), vendor and PO candidates with score breakdown, what was written (ledger commit and balance before -> after, review item, draft email marked "nothing is sent"), cost.
- `src/runState.ts`: the pure reducer (duplicates from a reconnect are ignored by seq; a failed run marks the running stage failed).
- Test fixtures in `src/test/fixtures/`: SSE bodies and run views recorded from the real backend on replay (SuperStore 10963, the IQ scan) plus three labelled synthetic ones generated offline (`synthetic_approve` = the controlled variant, `synthetic_request_info` = invoice number removed, `synthetic_failed` = injected act-stage error).

## What changed (M4 stage 3)
- `app/api/sse.py` + `GET /api/runs/{id}/events`: SSE tailing `audit_events` by seq (SPEC section 11 item 71): queued / audit / ping / end / rejected frames, `Last-Event-ID` resume, batches of 500, disconnect stops polling.
- The in-progress test runs the app under a real uvicorn server (the TestClient buffers streams): with the run held inside the match stage, every event up to `stage_started(match)` has already arrived; then the rest and `end`.

## What changed (M4 stage 2)
- `app/api/`: `main.py` (app factory), `routes.py`, `views.py` (read models from SQLite), `worker.py` (one run at a time), `uploads.py` (safe names, size-capped copy, the ingest acceptance check), `clients.py` (client by mode; `OfflineClient`), `serve.py`.
- Endpoints: `GET /api/health`, `POST /api/runs` (202 + run id), `GET /api/runs`, `GET /api/runs/{id}` (the whole run view), `GET /api/runs/{id}/pages/{n}`.
- `python -m app.api.serve (--replay DIR | --live [--record DIR] | --offline) [--db PATH] [--reset-demo] [--host] [--port]`: refuses to start without a mode (exit 5).
- Config: `api_host`, `api_port`, `api_cors_origins`, `api_upload_dir`, `api_busy_timeout_ms`, `sse_poll_ms`, `sse_heartbeat_s`, `api_recent_runs_max`. Dependencies: `uvicorn`, `python-multipart`; dev `httpx`. `data/uploads/` is gitignored.
- Smoke check on the real server (replay, scratch DB): the IQ scan uploaded with curl went queued -> completed, review, currency INR, all 7 stages with timings; its page image was served as PNG.

## Decisions I need from the user
None right now.

## Assumptions added to SPEC section 11
69 (stage timing events), 70 (the API), 71 (the live stream). Earlier: 61-68 (M3).

## Known risks or gaps
- Node here is 22.19; jsdom is pinned to 29 (jsdom 30 needs Node 22.22+).
- Without a declared Content-Length, the multipart parser buffers the upload (to a temp file) before the size check can stop it; browsers always send the length.
- The explain card completes a moment before the explanation text appears (it is written in the act transaction, as in M3).
- The real explainer/drafter have never run live. Extraction varies between calls.
- The IQ manifest entry is still an unverified draft (`currency` should be `"INR"` when you verify it).
