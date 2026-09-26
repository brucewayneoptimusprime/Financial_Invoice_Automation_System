# Deploying the invoice agent: Render (backend, free tier) + Vercel (frontend)

The backend (FastAPI + SQLite) runs on **Render's free instance type**; the frontend (React + Vite) is a static build on **Vercel**
that calls the backend's URL. Local development does not change: nothing below is needed to run it on your machine.

> **Free tier, accepted trade-off: the backend's data is temporary.** Render's free web services have no persistent disk, no Shell
> and no one-off jobs. Everything the running backend writes is lost when it spins down (after 15 minutes without traffic),
> restarts or redeploys; each start begins again from a fresh demo database created by the build. Read
> [What is lost, and when](#what-is-lost-and-when) before a demo.
>
> **The server never creates, resets or migrates the database by itself.** The demo database is created by the explicit
> `python -m app.db.reset --demo` in `render.yaml`'s **build** command; if it were ever missing, the server refuses to start and says so.

## 1. Before you start
- A GitHub repository with this code pushed to it.
- A **Render** account (free; no card needed for the free instance type).
- A **Vercel** account.
- Your **Anthropic API key**.
- A long random **access token**. Anyone with it can use the deployed app (upload invoices, which spends your key; approve or reject
  reviews; create POs), so keep it private. Generate one:
  ```
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  ```

## 2. Render: the backend
1. Render dashboard > **New** > **Blueprint** > pick the repository. Render reads `render.yaml` from the repository root: one web
   service `invoice-agent-api` on the `free` plan.
   - build: `pip install -e . && python -m app.db.reset --demo` (installs the app, then creates the demo database)
   - start: `python -m app.api.serve --host 0.0.0.0 --port $PORT`
   - health check: `/health`
2. Render asks for the values marked `sync: false`:

   | Variable | Value |
   |---|---|
   | `ANTHROPIC_API_KEY` | your Anthropic key |
   | `ACCESS_TOKEN` | the token from step 1 |
   | `API_CORS_ORIGINS` | leave empty for now (step 4) |

   Already set by `render.yaml` (change them in the dashboard if you want):

   | Variable | Value | Meaning |
   |---|---|---|
   | `SERVE_MODE` | `live` | real extraction (about $0.02-0.03 per invoice); `offline` = no model at all |
   | `DATA_DIR` | `data/render` | where the database, run folders, uploads and PO drafts live (relative to the repository root, inside the build output) |
   | `COST_CEILING_PER_SESSION_USD` | `1.00` | spend limit per server process; a new process (restart, spin-up, deploy) starts a new $1.00 |
   | `PYTHON_VERSION` | `3.12.7` | |

3. **Deploy.** The build log ends with `Database reset to seed state: data/render/app.db`; the service log shows `LIVE MODE: ...`.
4. Copy the service URL from the top of the page, e.g. `https://invoice-agent-api.onrender.com`, and check it:
   `https://invoice-agent-api.onrender.com/health` should answer `{"status": "ok", "db": "ok", "schema_version": 2, "mode": "live"}`.

## 3. Vercel: the frontend
1. Vercel dashboard > **Add New** > **Project** > import the same repository.
2. **Root Directory: `frontend`** (Vercel detects Vite; `frontend/vercel.json` sets the build command `npm run build`, the output
   folder `dist`, and the rule that sends every app route such as `/review/7` to `index.html`).
3. **Environment Variables:** `VITE_API_BASE` = the Render URL from step 2.4, with no trailing slash
   (`https://invoice-agent-api.onrender.com`). Tick **Production** (and **Preview** if you want preview deployments to work too).
   It is a build-time value: after changing it, redeploy.
4. **Deploy.** Copy the Vercel URL, e.g. `https://invoice-agent.vercel.app`.

## 4. Back to Render: allow the frontend
1. Service > **Environment** > `API_CORS_ORIGINS` = the Vercel URL (`https://invoice-agent.vercel.app`). Several are allowed,
   comma-separated.
2. Optional, for Vercel preview deployments: `API_CORS_ORIGIN_REGEX` = `https://invoice-agent-[a-z0-9-]+\.vercel\.app`
   (adjust to your project name).
3. Save; Render redeploys (which, on the free tier, also starts again from the fresh demo database).

## 5. Check the deployment
1. Open the Vercel URL. The app asks for the **access token**; paste it (it is kept for that browser tab only).
2. The dashboard loads with the demo purchase orders; the badge in the top right says **LIVE · paid API calls**.
3. Upload one invoice (about $0.03) and watch it run; check the Review queue and the Purchase orders pages.
4. `https://<render-url>/health` answers ok. `https://<render-url>/api/health` without the token answers 401 (as intended).

## What is lost, and when
**This is the one place that describes the free tier's temporary storage; it applies to everything the backend stores.**

Everything the backend writes at runtime lives under `DATA_DIR` (`data/render`) on the service's ephemeral filesystem:

| What | Where |
|---|---|
| the SQLite database: runs, audit trail, invoices, review decisions, ledger commits and allocations, purchase orders and vendors you entered | `data/render/app.db` |
| run folders: the stored copy of each uploaded invoice and its rendered page images | `data/render/runs/` |
| temporary upload copies (already deleted after each run) | `data/render/uploads/` |
| PO drafts made from text or documents | `data/render/po_drafts/` |

Nothing else is stored anywhere; no other part of the app assumes a persistent disk.

**All of it is lost, together, whenever the service:**
- **spins down**: Render's free tier stops the service after **15 minutes without any request**. The next request wakes it,
  which takes about a minute (the first page load after a pause is slow);
- **restarts** (for example after an environment-variable change, or Render's own maintenance);
- **redeploys** (a new commit, or a Manual Deploy).

Each start then begins from the build output: a **fresh demo dataset** (the SuperStore and IQ vendors and purchase orders), with no
runs, no reviews and no POs you added. Because the database and the run folders are reset together, nothing is ever left pointing at
a missing file. The $1.00 spend ceiling also starts again on each start.

**Before a demo or interview:**
1. Render dashboard > the service > **Manual Deploy** > **Deploy latest commit**. The build re-creates the demo database, so you
   start from known, correct data (a plain restart does the same, from the last build).
2. A minute before you present, open `https://<render-url>/health` once so the service is awake (a cold start takes about a
   minute), then keep using it: 15 minutes without any request puts it to sleep again and clears whatever you showed.
3. Anything you want to show "already processed" must be processed during the same awake period.

## 6. Operating it
- **Reset to the demo dataset:** there is no Shell on the free tier; **Manual Deploy** (or a restart) is the reset.
- **Migrations:** not needed on the free tier: every build creates the current schema from scratch.
- **Spend:** the dashboard shows the LLM spend since the last start; the ceiling is `COST_CEILING_PER_SESSION_USD` per start.
- **Rotate the access token:** change `ACCESS_TOKEN` on Render (it restarts, which also resets the data); every browser tab is asked
  for the new token.
- **Offline demo:** set `SERVE_MODE=offline` to show the app without any model calls (extraction then degrades to review).

## 7. If you later move to a paid plan with a disk
A paid instance type can attach a persistent disk and has a Shell; then the data survives restarts and deploys:
1. Add a disk (e.g. 1 GB) mounted at `/var/data`; set `DATA_DIR=/var/data`; change `plan` to a paid type.
2. **Remove the seed from the build command** (`buildCommand: pip install -e .`), otherwise every deploy would still start from
   a build-time database instead of the disk's.
3. Create the database ONCE, by hand, in the service's **Shell**: `python -m app.db.reset --demo` (demo data) or
   `python -m app.db.init_db` (empty). Until then the server refuses to start and says so.
4. Later: reset = `python -m app.db.reset --demo` in the Shell (destroys the data); after an upgrade that needs it,
   `python -m app.db.migrate` (it backs the database up first). Backups: Render disk snapshots.

## 8. Local development (unchanged)
Nothing above is needed locally; no environment variable has to be set. Window 1:
`cd C:\Zamp_ai_Automation; .\.venv\Scripts\Activate.ps1; cd backend; python -m app.api.serve --replay ..\data\recordings --reset-demo`.
Window 2: `cd C:\Zamp_ai_Automation\frontend; npm run dev`. Open http://localhost:5173.
