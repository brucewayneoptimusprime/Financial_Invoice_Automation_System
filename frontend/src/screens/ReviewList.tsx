import { useEffect, useState } from "react";
import { listReview } from "../api";
import { Chip } from "../components/common";
import { money, when } from "../format";
import { reviewReasons } from "../reasons";
import { linkProps } from "../router";
import type { ReviewListItem } from "../types";

// One item at a time: the list only opens items; it has no selection and no bulk action.
export function ReviewListScreen() {
  const [tab, setTab] = useState<"open" | "resolved">("open");
  const [items, setItems] = useState<ReviewListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stop = false;
    setItems(null);
    const load = () => listReview(tab).then((r) => { if (!stop) { setItems(r.items); setError(null); } })
                                      .catch(() => { if (!stop) setError("Could not load the review queue. Is the API running?"); });
    load();
    const t = window.setInterval(load, 5000);
    return () => { stop = true; window.clearInterval(t); };
  }, [tab]);

  return (
    <div className="review-list">
      <div className="page-head">
        <div>
          <h1>Review queue</h1>
          <p className="dim">Invoices the rules held for a person. Open one to approve or reject it; each is handled on its own.</p>
        </div>
      </div>
      <div className="tabs" role="tablist">
        {(["open", "resolved"] as const).map((t) => (
          <button key={t} type="button" role="tab" aria-selected={tab === t} className={`tab ${tab === t ? "active" : ""}`} onClick={() => setTab(t)}>
            {t === "open" ? "Open" : "Resolved"}
          </button>
        ))}
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {items === null ? <p className="dim">Loading…</p> : items.length === 0 ? (
        <p className="dim">{tab === "open" ? "Nothing is waiting for review." : "No item has been resolved yet."}</p>
      ) : (
        <ul className="queue">
          {items.map((i) => (
            <li key={i.id}>
              <a {...linkProps(`/review/${i.id}`)} className="queue-row">
                <div className="queue-main">
                  <div className="queue-title">
                    <strong>{i.vendor ?? "Unknown vendor"}</strong> · invoice {i.invoice_number ?? "(no number)"} · {money(i.total, i.currency)}
                    {i.po_number && <span className="tag">{i.po_number}</span>}
                  </div>
                  <div className="queue-reason">{reviewReasons(i.reason).map((r) => r.text).join(" ")}</div>
                  <div className="dim small">{i.source_file} · {when(i.queued_at)}</div>
                </div>
                <div className="queue-side">
                  {i.status === "resolved" ? <Chip tone={i.resolution === "approved" ? "pass" : "fail"}>{i.resolution}</Chip>
                    : !i.can_approve ? <Chip tone="muted">cannot approve</Chip>
                    : i.lines_needing_input > 0 ? <Chip tone="flag">{i.lines_needing_input} line choice{i.lines_needing_input === 1 ? "" : "s"}</Chip>
                    : <Chip tone="info">ready</Chip>}
                </div>
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
