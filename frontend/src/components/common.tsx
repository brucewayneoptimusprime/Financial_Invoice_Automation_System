import { useId, useState, type ReactNode } from "react";
import { humanize } from "../format";

export function Chip({ tone, children, title }: { tone: string; children: ReactNode; title?: string }) {
  return <span className={`chip chip-${tone}`} title={title}>{children}</span>;
}

export function Section({ title, aside, children, id }: { title: string; aside?: ReactNode; children: ReactNode; id?: string }) {
  return (
    <section className="section" id={id} aria-labelledby={id ? `${id}-h` : undefined}>
      <div className="section-head">
        <h2 id={id ? `${id}-h` : undefined}>{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

export function Confidence({ value, model }: { value: number; model?: number | null }) {
  const pctText = `${Math.round(value * 100)}%`;
  const tone = value >= 0.8 ? "pass" : value >= 0.5 ? "flag" : "fail";
  const capped = model !== null && model !== undefined && model > value + 0.001;
  return (
    <span className="conf" title={capped ? `Effective ${pctText}; the model reported ${Math.round(model! * 100)}% (capped by the evidence check)` : `Confidence ${pctText}`}>
      <span className="conf-bar" aria-hidden="true"><span className={`conf-fill tone-${tone}`} style={{ width: pctText }} /></span>
      <span className="conf-num">{pctText}</span>
      {capped && <span className="conf-model">model {Math.round(model! * 100)}%</span>}
    </span>
  );
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function Value({ v, depth }: { v: unknown; depth: number }) {
  if (v === null || v === undefined) return <span className="dim">—</span>;
  if (typeof v === "boolean") return <span>{v ? "yes" : "no"}</span>;
  if (typeof v === "number") return <span className="num">{Number.isInteger(v) ? v : v.toFixed(v < 1 ? 4 : 2)}</span>;
  if (typeof v === "string") return <span className={/^-?\d+(\.\d+)?$/.test(v) ? "num" : undefined}>{v || <span className="dim">(empty)</span>}</span>;
  if (Array.isArray(v)) {
    if (v.length === 0) return <span className="dim">none</span>;
    if (v.every((x) => typeof x !== "object" || x === null)) return <span>{v.map(String).join(", ")}</span>;
    return (
      <ol className="kv-list">
        {v.map((x, i) => <li key={i}><Value v={x} depth={depth + 1} /></li>)}
      </ol>
    );
  }
  if (isPlainObject(v)) return depth > 3 ? <code>{JSON.stringify(v)}</code> : <DetailList data={v} depth={depth + 1} />;
  return <span>{String(v)}</span>;
}

// Readable key/value rows for an event's `detail` (nested objects become nested lists, never raw JSON).
export function DetailList({ data, depth = 0, hide = [] }: { data: Record<string, unknown>; depth?: number; hide?: string[] }) {
  const entries = Object.entries(data).filter(([k]) => !hide.includes(k));
  if (entries.length === 0) return <span className="dim">no detail</span>;
  return (
    <dl className={`kv kv-${Math.min(depth, 2)}`}>
      {entries.map(([k, v]) => (
        <div className="kv-row" key={k}>
          <dt>{humanize(k)}</dt>
          <dd><Value v={v} depth={depth} /></dd>
        </div>
      ))}
    </dl>
  );
}

export function Disclosure({ summary, children, defaultOpen = false, className = "" }:
  { summary: ReactNode; children: ReactNode; defaultOpen?: boolean; className?: string }) {
  const [open, setOpen] = useState(defaultOpen);
  const id = useId();
  return (
    <div className={`disclosure ${open ? "open" : ""} ${className}`}>
      <button type="button" className="disclosure-btn" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>
        <span className="caret" aria-hidden="true" />
        {summary}
      </button>
      {open && <div id={id} className="disclosure-body">{children}</div>}
    </div>
  );
}
