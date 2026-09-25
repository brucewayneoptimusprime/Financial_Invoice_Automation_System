import { useEffect, useRef } from "react";
import { pageUrl } from "../api";

export interface PageTarget {
  page: number;
  label: string;
  sourceText: string | null;
}

// The rendered page beside the evidence it supports. Esc, the close button or a click outside closes it.
export function PageViewer({ runId, target, pages, onClose, onPage }:
  { runId: string; target: PageTarget; pages: number[]; onClose: () => void; onPage: (n: number) => void }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const exists = pages.includes(target.page);
  return (
    <div className="viewer-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="viewer" role="dialog" aria-modal="true" aria-label={`Page ${target.page}`}>
        <div className="viewer-head">
          <div>
            <div className="viewer-title">{target.label} · page {target.page}</div>
            {target.sourceText && <q className="viewer-quote">{target.sourceText}</q>}
          </div>
          <div className="viewer-actions">
            {pages.length > 1 && pages.map((n) => (
              <button key={n} type="button" className={`pill ${n === target.page ? "active" : ""}`} onClick={() => onPage(n)}>
                {n}
              </button>
            ))}
            <button ref={closeRef} type="button" className="btn-ghost" onClick={onClose} aria-label="Close page viewer">Close</button>
          </div>
        </div>
        <div className="viewer-img">
          {exists ? <img src={pageUrl(runId, target.page)} alt={`Rendered page ${target.page} of the invoice`} />
                  : <p className="dim">Page {target.page} was not rendered.</p>}
        </div>
      </div>
    </div>
  );
}
