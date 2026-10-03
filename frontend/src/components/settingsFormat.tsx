import type { SettingField, SettingKind, SettingValue } from "../types";
import { Chip } from "./common";

// How a setting reads on screen, and the client-side range check (the server checks again and is the authority).

export function showSetting(kind: SettingKind, value: SettingValue | null | undefined, currency?: string | null): string {
  if (value === null || value === undefined || value === "") return "—";
  if (kind === "percent") return `${Number(value).toFixed(2)}%`;
  if (kind === "money") return `${currency ? `${currency} ` : ""}${Number(value).toFixed(2)}`;
  if (kind === "mode") return value === "greater_of" ? "Either limit (the larger allowance)" : "Both limits (the smaller allowance)";
  if (kind === "threshold") return Number(value).toFixed(2);
  return `${value} day${Number(value) === 1 ? "" : "s"}`;
}

// A plain sentence of the range, e.g. "0 to 25 %".
export function rangeText(f: SettingField): string {
  if (f.kind === "mode") return "Both limits or either limit";
  const unit = f.kind === "percent" ? " %" : f.kind === "days" ? " days" : "";
  return `${f.min} to ${f.max}${unit}${f.kind === "money" ? " (whole cents)" : ""}`;
}

// null when the text is acceptable for the field, else the reason (mirrors the API: range, step, number).
export function checkSetting(f: SettingField, text: string): string | null {
  if (f.kind === "mode") return f.options?.includes(text) ? null : "Choose both limits or either limit.";
  if (text.trim() === "") return "Enter a value.";
  const n = Number(text);
  if (!Number.isFinite(n)) return "Enter a number.";
  if (f.min !== null && n < Number(f.min)) return `At least ${f.min}.`;
  if (f.max !== null && n > Number(f.max)) return `At most ${f.max}.`;
  if (f.step !== null) {
    const steps = n / Number(f.step);
    if (Math.abs(steps - Math.round(steps)) > 1e-9) return f.kind === "days" ? "Whole days only." : `In steps of ${f.step}.`;
  }
  return null;
}

export function toPayload(f: SettingField, text: string): SettingValue {
  if (f.kind === "mode") return text;
  if (f.kind === "money") return Number(text).toFixed(2);
  if (f.kind === "days") return Math.round(Number(text));
  return Number(text);
}

export function LooserMark({ title = "Looser than the global default" }: { title?: string }) {
  return <Chip tone="flag" title={title}>looser than default</Chip>;
}

export function SettingsNote() {
  return <p className="dim small">Changes apply to invoices processed from now on. Past decisions keep the settings they were made with.</p>;
}
