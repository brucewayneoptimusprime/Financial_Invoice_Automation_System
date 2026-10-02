"""The six real invoices processed twice, on two fresh demo databases: once uploaded through POST /api/runs and once imported from the
fake Gmail inbox through search + import. Both use the same recorded extract-v5 replies (picked by file hash) and templates for the
explainer and drafter, so any difference can only come from the import path.

    cd backend && python -m tests.gmail.regression        prints the comparison table (no network, no model call)
"""
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.pipeline.runner import run_pipeline
from tests.api.helpers import build_app
from tests.extraction.real import REAL, real_pdf
from tests.gmail.helpers import gmail_settings
from tests.pipeline.helpers import scripted

# fixture name -> (fake inbox message, part, the file name as sent / uploaded)
SIX = {
    "superstore_10963": ("fake-ss-10963", "1", "invoice_Scot Wooten_10963.pdf"),
    "superstore_24429": ("fake-ss-24429", "1", "invoice_Maria Zettner_24429.pdf"),
    "superstore_14021": ("fake-ss-two", "1", "invoice_Bill Eplett_14021.pdf"),
    "superstore_14130": ("fake-ss-two", "2", "invoice_Liz Thompson_14130.pdf"),
    "superstore_6459": ("fake-ss-6459", "1", "invoice_Darren Koutras_6459.pdf"),
    "iq_electronics": ("fake-iq", "1", "image_based_invoice.jpg"),
}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def v5_reply(name: str) -> dict:
    return json.loads((REAL / f"{name}.v5.reply.json").read_text(encoding="utf-8"))


class HashRuns:
    """run_fn for the worker: the recorded extract-v5 reply is chosen by the file's SHA-256, so an upload and a Gmail import of the
    same bytes get the same reply whatever the file is called. Unknown files get the server's (offline) client."""

    def __init__(self):
        self.replies = {sha256_file(real_pdf(name)): v5_reply(name) for name in SIX}
        self.calls: list[tuple[str, dict | None]] = []

    def __call__(self, path, conn, *, client, settings, run_id, source_name, provenance=None):
        self.calls.append((source_name, provenance))
        reply = self.replies.get(sha256_file(path))
        return run_pipeline(path, conn, client=client if reply is None else scripted(reply), settings=settings, run_id=run_id,
                            source_name=source_name, provenance=provenance)


def outcome(view: dict) -> dict:
    """What must be identical between the two paths."""
    return {
        "decision": view["decision"],
        "status": view["run"]["status"],
        "matched_po": view["match"]["matched_po"],
        "match_status": view["match"]["status"],
        "triggered": sorted(f"{r['rule_id']}:{r['outcome_key']}" for r in view["rules"] if r["outcome"] in ("flag", "fail")),
        "rule_results": len(view["rules"]),
        "total": (view["invoice"] or {}).get("total"),
        "file_hash": (view["invoice"] or {}).get("file_hash"),
        "source_file": view["run"]["source_file"],
        "line_mode": (view.get("line_matches") or {}).get("mode"),
        "review_items": len(view["actions"]["review"]),
        "ledger": view["actions"]["ledger"] is not None,
    }


def via_upload(tmp: Path) -> dict[str, dict]:
    tmp.mkdir(parents=True, exist_ok=True)
    app, worker, db, settings = build_app(tmp, settings=gmail_settings(tmp), run_fn=HashRuns())
    out = {}
    with TestClient(app) as c:
        ids = {}
        for name, (_, _, filename) in SIX.items():
            r = c.post("/api/runs", files={"file": (filename, real_pdf(name).read_bytes(), "application/octet-stream")})
            assert r.status_code == 202, r.text
            ids[name] = r.json()["run_id"]
        assert worker.wait_idle(180)
        for name, rid in ids.items():
            out[name] = outcome(c.get(f"/api/runs/{rid}").json())
    return out


def via_gmail(tmp: Path) -> tuple[dict[str, dict], dict]:
    tmp.mkdir(parents=True, exist_ok=True)
    app, worker, db, settings = build_app(tmp, settings=gmail_settings(tmp), run_fn=HashRuns())
    out, views = {}, {}
    with TestClient(app) as c:
        search = c.post("/api/gmail/search", json={"query": "after:2026/07/01 -subject:ignore"}).json()
        items = [{"message_id": m, "part_id": p} for m, p, _ in SIX.values()]
        r = c.post("/api/gmail/import", json={"search_id": search["search_id"], "items": items, "confirm": True})
        assert r.status_code == 200, r.text
        results = r.json()["items"]
        assert [x["status"] for x in results] == ["queued"] * 6, results
        assert worker.wait_idle(180)
        for name, res in zip(SIX, results):
            views[name] = c.get(f"/api/runs/{res['run_id']}").json()
            out[name] = outcome(views[name])
    return out, views


def table(upload: dict, gmail: dict) -> str:
    rows = ["| Invoice | Upload: decision | Gmail: decision | PO | Triggered checks (identical on both paths) | Results | Same? |",
            "|---|---|---|---|---|---|---|"]
    for name in SIX:
        u, g = upload[name], gmail[name]
        rows.append(f"| {name} | {u['decision']} | {g['decision']} | {g['matched_po'] or '-'} | {', '.join(g['triggered']) or '-'} | "
                    f"{g['rule_results']} | {'yes' if u == g else 'NO'} |")
    return "\n".join(rows)


if __name__ == "__main__":
    import os
    import tempfile

    for secret in ("ANTHROPIC_API_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "OAUTH_ENCRYPTION_KEY"):
        os.environ[secret] = ""                                   # as in conftest: no real key or OAuth client, ever
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        u = via_upload(Path(d) / "upload")
        g, _ = via_gmail(Path(d) / "gmail")
    print(table(u, g))
