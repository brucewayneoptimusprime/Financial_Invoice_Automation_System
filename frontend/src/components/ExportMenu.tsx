import { useEffect, useId, useRef, useState, type MouseEvent } from "react";
import { ApiError } from "../api";

// "Export" (never "Share": nothing is sent; the browser saves a file). One button with a download icon that opens a small menu:
// optionally the level, then the format. Choosing a format downloads. Clicks never bubble to the row around it.

export type ExportFormat = "pdf" | "docx" | "xlsx" | "csv";
export type ExportLevel = "financial" | "full";

export const FORMATS: { id: ExportFormat; label: string }[] = [
  { id: "pdf", label: "PDF" }, { id: "docx", label: "Word (.docx)" }, { id: "xlsx", label: "Excel (.xlsx)" }, { id: "csv", label: "CSV" },
];

export function DownloadIcon() {
  return (
    <svg className="export-icon" width="14" height="14" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      <path d="M8 1.5v8.2M4.6 6.4 8 9.8l3.4-3.4M2.5 11.5v2h11v-2" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"
            strokeLinejoin="round" />
    </svg>
  );
}

export interface ExportMenuProps {
  label: string;
  ariaLabel?: string;
  small?: boolean;
  withLevels?: boolean;
  disabled?: boolean;
  onExport(format: ExportFormat, level: ExportLevel): Promise<unknown>;
}

export function ExportMenu({ label, ariaLabel, small = false, withLevels = false, disabled = false, onExport }: ExportMenuProps) {
  const [open, setOpen] = useState(false);
  const [level, setLevel] = useState<ExportLevel>("financial");
  const [busy, setBusy] = useState<ExportFormat | null>(null);
  const [error, setError] = useState<string | null>(null);
  const button = useRef<HTMLButtonElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { setOpen(false); button.current?.focus(); }
    };
    const onDown = (e: Event) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    return () => { document.removeEventListener("keydown", onKey); document.removeEventListener("mousedown", onDown); };
  }, [open]);

  const stop = (e: MouseEvent) => e.stopPropagation();

  const choose = async (format: ExportFormat) => {
    setBusy(format);
    setError(null);
    try {
      await onExport(format, level);
      setOpen(false);
      button.current?.focus();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "The export failed. Is the API running?");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="export-menu" ref={box} onClick={stop}>
      <button type="button" ref={button} className={`btn-ghost export-btn${small ? " small-btn" : ""}`} aria-haspopup="true"
              aria-expanded={open} aria-controls={open ? menuId : undefined} aria-label={ariaLabel} disabled={disabled}
              onClick={(e) => { e.stopPropagation(); setOpen((o) => !o); setError(null); }}>
        <DownloadIcon /> {label}
      </button>
      {open && (
        <div className="export-pop" id={menuId} role="group" aria-label={ariaLabel ?? label}>
          {withLevels && (
            <fieldset className="export-levels">
              <legend>Level</legend>
              <label><input type="radio" name={`${menuId}-level`} checked={level === "financial"} onChange={() => setLevel("financial")} /> Financial</label>
              <label><input type="radio" name={`${menuId}-level`} checked={level === "full"} onChange={() => setLevel("full")} /> Full (with metadata)</label>
            </fieldset>
          )}
          <div className="export-formats" role="group" aria-label="Format">
            {FORMATS.map((f) => (
              <button key={f.id} type="button" className="btn-ghost small-btn" disabled={busy !== null} onClick={() => choose(f.id)}>
                {busy === f.id ? "Preparing…" : f.label}
              </button>
            ))}
          </div>
          <p className="dim small">Saved to your computer. Nothing is sent anywhere.</p>
          {error && <p className="error small" role="alert">{error}</p>}
        </div>
      )}
    </div>
  );
}
