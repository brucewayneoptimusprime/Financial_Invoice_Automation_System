# Deploying the invoice agent: Render (backend) + Vercel (frontend)

The backend (FastAPI + SQLite) runs on **Render** with a persistent disk; the frontend (React + Vite) is a static build on **Vercel**
that calls the backend's URL. Local development does not change: nothing below is needed to run it on your machine.

> **Nothing resets or migrates the database automatically.** Deploys and restarts only open the existing database on the disk.
> Creating it, resetting it and migrating it are manual commands you run in the Render Shell (steps 2.4 and 6).

## 1. Before you start
- A GitHub repository with this code pushed to it.
- A **Render** account. The service needs a **paid instance type** (`starter` or larger): free web services have no persistent
  disk, so the SQLite database would be lost on every deploy.
- A **Vercel** account.
- Your **Anthropic API key**.
- A long random **access token**. Anyone with it can use the deployed app (upload invoices, which spends your key; approve or reject
  reviews; create POs), so keep it private. Generate one:
  ```
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  ```

## 2. Render: the backend
1. Render dashboard > **New** > **Blueprint** > pick the repository. Render reads `render.yaml` from the repository root: one web
   service `invoice-agent-api`, Python, start command `python -m app.api.serve --host 0.0.0.0 --port $PORT`, health check `/health`,
   and a 1 GB disk mounted at `/var/data`.
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
   | `DATA_DIR` | `/var/data` | the disk: database, run folders, uploads, PO drafts |
   | `COST_CEILING_PER_SESSION_USD` | `1.00` | spend limit per server process; it resets on every restart or deploy |
   | `PYTHON_VERSION` | `3.12.7` | |

3. **Deploy.** The first start stops on purpose: *"The database /var/data/app.db does not exist"*, and the health check fails. That
   is expected: the database is never created automatically.
4. Open the service's **Shell** tab and run ONE of these, once:
   - the demo dataset (the SuperStore and IQ vendors and POs used in development):
     ```
     python -m app.db.reset --demo
     ```
   - an empty database (schema and builtin rules only; enter your own vendors and POs in the app):
     ```
     python -m app.db.init_db
     ```
   Both use `DATA_DIR`, so they write `/var/data/app.db`.
5. **Manual Deploy** > **Restart service**. The logs show `LIVE MODE: ...`; the health check turns green.
6. Copy the service URL from the top of the page, e.g. `https://invoice-agent-api.onrender.com`, and check it:
   `https://invoice-agent-api.onrender.com/health` should answer `{"status": "ok", "db": "ok", "schema_version": 2, "mode": "live"}`.

## 3. Vercel: the frontend
1. Vercel dashboard > **Add New** > **Project** > import the same repository.
2. **Root Directory: `frontend`** (Vercel detects Vite; `frontend/vercel.json` sets the build command `npm run build`, the output
   folder `dist`, and the rule that sends every app route such as `/review/7` to `index.html`).
3. **Environment Variables:** `VITE_API_BASE` = the Render URL from step 2.6, with no trailing slash
   (`https://invoice-agent-api.onrender.com`). Tick **Production** (and **Preview** if you want preview deployments to work too).
   It is a build-time value: after changing it, redeploy.
4. **Deploy.** Copy the Vercel URL, e.g. `https://invoice-agent.vercel.app`.

## 4. Back to Render: allow the frontend
1. Service > **Environment** > `API_CORS_ORIGINS` = the Vercel URL (`https://invoice-agent.vercel.app`). Several are allowed,
   comma-separated.
2. Optional, for Vercel preview deployments: `API_CORS_ORIGIN_REGEX` = `https://invoice-agent-[a-z0-9-]+\.vercel\.app`
   (adjust to your project name).
3. Save; Render restarts the service.

## 5. Check the deployment
1. Open the Vercel URL. The app asks for the **access token**; paste it (it is kept for that browser tab only).
2. The dashboard loads; the badge in the top right says **LIVE · paid API calls**.
3. Upload one invoice (about $0.03) and watch it run; check the Review queue and the Purchase orders pages.
4. `https://<render-url>/health` answers ok. `https://<render-url>/api/health` without the token answers 401 (as intended).

## 6. Operating it
- **Deploys and restarts never touch the database.** It lives only on the disk at `/var/data/app.db`.
- **Reset to the demo dataset** (DESTROYS everything in the database): Shell > `python -m app.db.reset --demo`, then restart.
- **Migrate after an upgrade that needs it** (the service refuses to start and says so): Shell > `python -m app.db.migrate`
  (it copies the database to `app.db.v<N>-<time>.bak` first), then restart.
- **Backups:** Render disk snapshots, or copy `/var/data/app.db` from the Shell.
- **Spend:** the dashboard shows the LLM spend. The $1.00 ceiling is per server process and resets whenever the service restarts,
  so a restart gives another $1.00; lower or raise `COST_CEILING_PER_SESSION_USD` as you prefer.
- **Rotate the access token:** change `ACCESS_TOKEN` on Render (it restarts); every browser tab is asked for the new one.
- **Offline demo:** set `SERVE_MODE=offline` to show the app without any model calls (extraction then degrades to review).
- A disk means one instance and a short downtime on each deploy (Render's rule for services with disks).

## 7. Local development (unchanged)
Nothing above is needed locally; no environment variable has to be set. Window 1:
`cd C:\Zamp_ai_Automation; .\.venv\Scripts\Activate.ps1; cd backend; python -m app.api.serve --replay ..\data\recordings --reset-demo`.
Window 2: `cd C:\Zamp_ai_Automation\frontend; npm run dev`. Open http://localhost:5173.
