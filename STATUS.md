# STATUS

Last updated: 2026-09-25. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0-M3 complete; end-of-M3 re-record with extract-v5 done ($0.143; IQ currency now INR).
- **M4 (API + live run view): stages 1-2 of 5 done** (runner timing events; the HTTP API).
- Next: stage 3 (the SSE event stream).
- No live calls in M4 (owner runs `--live` after seeing the UI on replay).

## Test count and result
**1898 passed, 0 failed, 2 deselected** (`pytest -W error`); stage 2 added 44 (`tests/api`).

## What changed (M4 stage 2)
- `app/api/`: `main.py` (app factory), `routes.py`, `views.py` (read models from SQLite), `worker.py` (one run at a time), `uploads.py` (safe names, size-capped copy, the ingest acceptance check), `clients.py` (client by mode; `OfflineClient`), `serve.py`.
- Endpoints: `GET /api/health`, `POST /api/runs` (202 + run id), `GET /api/runs`, `GET /api/runs/{id}` (the whole run view), `GET /api/runs/{id}/pages/{n}`.
- `python -m app.api.serve (--replay DIR | --live [--record DIR] | --offline) [--db PATH] [--reset-demo] [--host] [--port]`: refuses to start without a mode (exit 5).
- Config: `api_host`, `api_port`, `api_cors_origins`, `api_upload_dir`, `api_busy_timeout_ms`, `sse_poll_ms`, `sse_heartbeat_s`, `api_recent_runs_max`. Dependencies: `uvicorn`, `python-multipart`; dev `httpx`. `data/uploads/` is gitignored.
- Smoke check on the real server (replay, scratch DB): the IQ scan uploaded with curl went queued -> completed, review, currency INR, all 7 stages with timings; its page image was served as PNG.

## Decisions I need from the user
None right now.

## Assumptions added to SPEC section 11
69 (stage timing events), 70 (the API). Earlier: 61-68 (M3).

## Known risks or gaps
- Without a declared Content-Length, the multipart parser buffers the upload (to a temp file) before the size check can stop it; browsers always send the length.
- The explain card completes a moment before the explanation text appears (it is written in the act transaction, as in M3).
- The real explainer/drafter have never run live. Extraction varies between calls.
- The IQ manifest entry is still an unverified draft (`currency` should be `"INR"` when you verify it).
