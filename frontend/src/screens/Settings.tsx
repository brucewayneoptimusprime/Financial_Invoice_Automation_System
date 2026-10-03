import { useEffect, useState } from "react";
import { ApiError, getSettings, listSettingsPOs, saveGlobalSettings, settingsHistory, type SettingsProblems } from "../api";
import { Chip, Section } from "../components/common";
import { checkSetting, LooserMark, rangeText, SettingsNote, showSetting, toPayload } from "../components/settingsFormat";
import { when } from "../format";
import { linkProps } from "../router";
import type { GlobalSettings, SettingsEvent, SettingsPORow } from "../types";

// Settings: (a) the global defaults every PO inherits, and (b) the purchase orders, each "default" or "custom". No model, no cost.

export function SettingsScreen() {
  const [g, setG] = useState<GlobalSettings | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [ruleEdits, setRuleEdits] = useState<Record<string, boolean>>({});
  const [problems, setProblems] = useState<SettingsProblems>({});
  const [message, setMessage] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [history, setHistory] = useState<SettingsEvent[]>([]);
  const [pos, setPos] = useState<SettingsPORow[] | null>(null);
  const [q, setQ] = useState("");
  const [error, setError] = useState<string | null>(null);

  const reload = () => {
    getSettings().then((x) => { setG(x); setError(null); }).catch((e) => setError(e instanceof ApiError ? e.message : "Could not load the settings."));
    settingsHistory({ limit: 20 }).then((h) => setHistory(h.events)).catch(() => {});
  };
  useEffect(reload, []);
  useEffect(() => {
    let stop = false;
    const t = window.setTimeout(() => listSettingsPOs(q).then((r) => !stop && setPos(r.pos)).catch(() => !stop && setPos([])), 200);
    return () => { stop = true; window.clearTimeout(t); };
  }, [q, g]);

  if (error) return <p className="error" role="alert">{error}</p>;
  if (!g) return <p className="dim">Loading…</p>;

  const local: Record<string, string | null> = {};
  for (const f of g.values) {
    const text = edits[f.key];
    if (text !== undefined) local[f.key] = checkSetting(f, text);
  }
  const hasLocalErrors = Object.values(local).some(Boolean);
  const changedValues = g.values.filter((f) => edits[f.key] !== undefined && !local[f.key] && toPayload(f, edits[f.key]) !== f.value);
  const changedRules = Object.entries(ruleEdits).filter(([id, v]) => g.rules.find((r) => r.id === id)?.enabled !== v);
  const dirty = changedValues.length + changedRules.length > 0;

  const save = async (body: Parameters<typeof saveGlobalSettings>[0]) => {
    setSaving(true);
    setMessage(null);
    setProblems({});
    try {
      const r = await saveGlobalSettings(body);
      if (r.ok) {
        setG(r.body);
        setEdits({});
        setRuleEdits({});
        setMessage(r.body.changed ? `Saved ${r.body.changed} change${r.body.changed === 1 ? "" : "s"}. They apply to invoices processed from now on.`
                                  : "Nothing changed.");
        settingsHistory({ limit: 20 }).then((h) => setHistory(h.events)).catch(() => {});
      } else {
        setProblems(r.body.problems ?? {});
        setMessage(r.body.message ?? "The settings were not saved.");
      }
    } finally {
      setSaving(false);
    }
  };

  const submit = () => save({ values: Object.fromEntries(changedValues.map((f) => [f.key, toPayload(f, edits[f.key])])),
                              rules: Object.fromEntries(changedRules) });

  return (
    <div className="settings">
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p className="dim">The rules every purchase order inherits, and the purchase orders that override them. The most specific wins.</p>
        </div>
      </div>

      <Section title="Global defaults" id="settings-global">
        <SettingsNote />
        <div className="settings-grid">
          {g.values.map((f) => {
            const text = edits[f.key] ?? String(f.value);
            const err = local[f.key] ?? problems[f.key];
            return (
              <div key={f.key} className="setting-row">
                <label htmlFor={`g-${f.key}`} className="setting-label">{f.label}</label>
                {f.kind === "mode" ? (
                  <select id={`g-${f.key}`} value={text} onChange={(e) => setEdits({ ...edits, [f.key]: e.target.value })}>
                    <option value="lesser_of">Both limits (stricter)</option>
                    <option value="greater_of">Either limit</option>
                  </select>
                ) : (
                  <input id={`g-${f.key}`} type="number" inputMode="decimal" value={text} min={f.min ?? undefined} max={f.max ?? undefined}
                         step={f.step ?? undefined} aria-invalid={err ? true : undefined} aria-describedby={`g-${f.key}-help`}
                         onChange={(e) => setEdits({ ...edits, [f.key]: e.target.value })} />
                )}
                <p className="dim small" id={`g-${f.key}-help`}>{f.help} Range: {rangeText(f)}. Built-in default: {showSetting(f.kind, f.builtin)}.
                  {f.value !== f.builtin && (
                    <> <button type="button" className="linkish" onClick={() => save({ restore: [f.key] })} disabled={saving}>Restore</button></>
                  )}
                </p>
                {err && <p className="error small" role="alert">{err}</p>}
              </div>
            );
          })}
        </div>
        <h3 className="settings-sub">Rules</h3>
        <ul className="rule-switches">
          {g.rules.map((r) => (
            <li key={r.id}>
              <label>
                <input type="checkbox" checked={r.locked ? true : (ruleEdits[r.id] ?? r.enabled)} disabled={r.locked}
                       onChange={(e) => setRuleEdits({ ...ruleEdits, [r.id]: e.target.checked })} aria-describedby={r.reason ? `why-${r.id}` : undefined} />
                <span>{r.name}</span> <code className="dim small">{r.id}</code>
              </label>
              {r.reason && <span className="dim small" id={`why-${r.id}`}>{r.reason}</span>}
              {problems[`rule:${r.id}`] && <span className="error small">{problems[`rule:${r.id}`]}</span>}
            </li>
          ))}
          {g.floors.map((f) => (
            <li key={f.id}>
              <label><input type="checkbox" checked disabled aria-describedby={`why-${f.id}`} /> <span>{f.name}</span></label>
              <span className="dim small" id={`why-${f.id}`}>{f.reason}</span>
            </li>
          ))}
        </ul>
        <div className="settings-actions">
          <button type="button" className="btn" disabled={!dirty || hasLocalErrors || saving} onClick={submit}>{saving ? "Saving…" : "Save global defaults"}</button>
          {dirty && <button type="button" className="btn-ghost" onClick={() => { setEdits({}); setRuleEdits({}); setProblems({}); }}>Cancel</button>}
          {message && <span className={Object.keys(problems).length ? "error small" : "dim small"} role="status">{message}</span>}
        </div>
      </Section>

      <Section title="Purchase orders" id="settings-pos">
        <input type="search" placeholder="Search PO number or vendor" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search purchase orders" />
        {pos === null ? <p className="dim">Loading…</p> : pos.length === 0 ? <p className="dim">No purchase orders match.</p> : (
          <ul className="run-list settings-po-list">
            {pos.map((p) => (
              <li key={p.id}>
                <a {...linkProps(`/settings/pos/${p.id}`)} className="run-link">
                  <span className="run-file">{p.po_number}<span className="dim"> · {p.vendor}</span></span>
                  <span className="run-meta">{p.currency}</span>
                  <span className="run-chips">
                    {p.custom ? <Chip tone="info">custom ({p.overrides})</Chip> : <Chip tone="muted">default</Chip>}
                    {p.looser && <LooserMark />}
                  </span>
                </a>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Recent changes" id="settings-history">
        {history.length === 0 ? <p className="dim">No settings have been changed yet.</p> : (
          <ul className="settings-history">
            {history.map((e) => (
              <li key={e.id}><span>{e.message}</span> <span className="dim small">{when(e.created_at)} · actor: {e.actor}</span></li>
            ))}
          </ul>
        )}
      </Section>
    </div>
  );
}
