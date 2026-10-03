import { useEffect, useState } from "react";
import { ApiError, getPOSettings, savePOSettings, settingsHistory, type SettingsProblems } from "../api";
import { Chip, Section } from "../components/common";
import { checkSetting, LooserMark, rangeText, SettingsNote, showSetting, toPayload } from "../components/settingsFormat";
import { when } from "../format";
import { linkProps } from "../router";
import type { POSettings, POSettingField, SettingsEvent } from "../types";

// The rules for ONE purchase order: each value inherits the global default or overrides it; "Reset to default" removes the override.

function StatusText({ f, currency }: { f: POSettingField; currency: string }) {
  return f.source === "overridden"
    ? <span>overridden: <strong>{showSetting(f.kind, f.value, f.kind === "money" ? currency : null)}</strong></span>
    : <span className="dim">inherits default ({showSetting(f.kind, f.default, f.kind === "money" ? currency : null)})</span>;
}

export function POSettingsScreen({ id }: { id: number }) {
  const [d, setD] = useState<POSettings | null>(null);
  const [edits, setEdits] = useState<Record<string, string | null>>({});        // null = reset to default
  const [ruleEdits, setRuleEdits] = useState<Record<string, boolean | null>>({});
  const [problems, setProblems] = useState<SettingsProblems>({});
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [history, setHistory] = useState<SettingsEvent[]>([]);

  useEffect(() => {
    getPOSettings(id).then(setD).catch((e) => setError(e instanceof ApiError && e.status === 404 ? "There is no such purchase order." : "Could not load the rules."));
    settingsHistory({ scope: "po", po_id: id, limit: 20 }).then((h) => setHistory(h.events)).catch(() => {});
  }, [id]);

  if (error) return <div className="empty"><p className="error" role="alert">{error}</p><p><a {...linkProps("/settings")}>← Settings</a></p></div>;
  if (!d) return <p className="dim">Loading…</p>;

  const local: Record<string, string | null> = {};
  for (const f of d.values) {
    const e = edits[f.key];
    if (typeof e === "string") local[f.key] = checkSetting(f, e);
  }
  const bad = Object.values(local).some(Boolean);
  const dirty = Object.keys(edits).length + Object.keys(ruleEdits).length > 0;

  const save = async () => {
    setSaving(true);
    setMessage(null);
    setProblems({});
    const values = Object.fromEntries(Object.entries(edits).map(([k, v]) => [k, v === null ? null : toPayload(d.values.find((f) => f.key === k)!, v)]));
    try {
      const r = await savePOSettings(id, { values, rules: ruleEdits });
      if (r.ok) {
        setD(r.body);
        setEdits({});
        setRuleEdits({});
        setMessage("Saved. The changes apply to invoices matched to this PO from now on.");
        settingsHistory({ scope: "po", po_id: id, limit: 20 }).then((h) => setHistory(h.events)).catch(() => {});
      } else {
        setProblems(r.body.problems ?? {});
        setMessage(r.body.message ?? "The rules were not saved.");
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="settings">
      <div className="run-title">
        <a {...linkProps("/settings")} className="back">← Settings</a>
        <h1>Rules for {d.po.po_number}</h1>
        <div className="run-sub">
          <span>{d.po.vendor}</span><span>{d.po.currency}</span>
          {d.overrides > 0 ? <Chip tone="info">custom ({d.overrides})</Chip> : <Chip tone="muted">default</Chip>}
          {d.looser && <LooserMark />}
          <a {...linkProps(`/pos/${d.po.id}`)}>Open the purchase order</a>
        </div>
      </div>
      <Section title="Values" id="po-settings-values">
        <SettingsNote />
        <div className="settings-grid">
          {d.values.map((f) => {
            const e = edits[f.key];
            const text = typeof e === "string" ? e : String(f.value);
            const err = local[f.key] ?? problems[f.key];
            return (
              <div key={f.key} className="setting-row" data-testid={`po-setting-${f.key}`}>
                <label htmlFor={`p-${f.key}`} className="setting-label">{f.label}</label>
                <p className="setting-status">
                  {e === null ? <span>will reset to the default ({showSetting(f.kind, f.default, f.kind === "money" ? d.po.currency : null)})</span>
                              : <StatusText f={f} currency={d.po.currency} />}
                  {f.looser && e !== null && <> <LooserMark /></>}
                </p>
                {f.kind === "mode" ? (
                  <select id={`p-${f.key}`} value={text} onChange={(ev) => setEdits({ ...edits, [f.key]: ev.target.value })}>
                    <option value="lesser_of">Both limits (stricter)</option>
                    <option value="greater_of">Either limit</option>
                  </select>
                ) : (
                  <input id={`p-${f.key}`} type="number" inputMode="decimal" value={text} min={f.min ?? undefined} max={f.max ?? undefined}
                         step={f.step ?? undefined} aria-invalid={err ? true : undefined}
                         onChange={(ev) => setEdits({ ...edits, [f.key]: ev.target.value })} />
                )}
                <p className="dim small">Range: {rangeText(f)}.{f.kind === "money" ? ` In ${d.po.currency}.` : ""}
                  {f.source === "overridden" && e !== null && (
                    <> <button type="button" className="linkish" onClick={() => setEdits({ ...edits, [f.key]: null })}>Reset to default</button></>
                  )}
                </p>
                {err && <p className="error small" role="alert">{err}</p>}
              </div>
            );
          })}
        </div>
      </Section>
      <Section title="Rules" id="po-settings-rules">
        <ul className="rule-switches">
          {d.rules.map((r) => {
            const e = ruleEdits[r.id];
            const choice = e === undefined ? (r.source === "overridden" ? (r.enabled ? "on" : "off") : "inherit") : e === null ? "inherit" : e ? "on" : "off";
            return (
              <li key={r.id} data-testid={`po-rule-${r.id}`}>
                <label htmlFor={`r-${r.id}`}><span>{r.name}</span> <code className="dim small">{r.id}</code></label>
                {r.locked ? <span className="dim small">On. {r.reason}</span> : (
                  <select id={`r-${r.id}`} value={choice} aria-label={`${r.name}: on, off or inherit`}
                          onChange={(ev) => setRuleEdits({ ...ruleEdits, [r.id]: ev.target.value === "inherit" ? null : ev.target.value === "on" })}>
                    <option value="inherit">Inherits default ({r.default ? "on" : "off"})</option>
                    <option value="on">On</option>
                    <option value="off">Off</option>
                  </select>
                )}
                {r.looser && e === undefined && <LooserMark title="Switched off here although it is on by default" />}
                {problems[`rule:${r.id}`] && <span className="error small">{problems[`rule:${r.id}`]}</span>}
              </li>
            );
          })}
          {d.floors.map((f) => <li key={f.id}><span>{f.name}</span> <span className="dim small">On. {f.reason}</span></li>)}
        </ul>
        <div className="settings-actions">
          <button type="button" className="btn" disabled={!dirty || bad || saving} onClick={save}>{saving ? "Saving…" : "Save rules for this PO"}</button>
          {dirty && <button type="button" className="btn-ghost" onClick={() => { setEdits({}); setRuleEdits({}); setProblems({}); }}>Cancel</button>}
          {message && <span className={Object.keys(problems).length ? "error small" : "dim small"} role="status">{message}</span>}
        </div>
      </Section>
      <Section title="Changes to this PO's rules" id="po-settings-history">
        {history.length === 0 ? <p className="dim">No changes yet.</p> : (
          <ul className="settings-history">{history.map((e) => <li key={e.id}>{e.message} <span className="dim small">{when(e.created_at)} · actor: {e.actor}</span></li>)}</ul>
        )}
      </Section>
    </div>
  );
}

// "Rules for this PO" on the PO page: the effective values at a glance, and the way into the same editor.
export function RulesForThisPO({ poId }: { poId: number }) {
  const [d, setD] = useState<POSettings | null>(null);
  // optional on the PO page: a failed or unexpected reply hides the section instead of breaking the page
  useEffect(() => { getPOSettings(poId).then((x) => setD(Array.isArray(x?.values) && Array.isArray(x?.rules) ? x : null)).catch(() => setD(null)); }, [poId]);
  if (!d) return null;
  const off = d.rules.filter((r) => !r.enabled);
  return (
    <Section title="Rules for this PO" id="po-rules"
             aside={<a {...linkProps(`/settings/pos/${poId}`)}>Edit rules for this PO</a>}>
      <p className="dim small">
        {d.overrides > 0 ? `${d.overrides} value${d.overrides === 1 ? "" : "s"} differ from the global defaults.` : "Everything inherits the global defaults."}
        {d.looser && <> <LooserMark /></>}
      </p>
      <dl className="kv">
        {d.values.map((f) => (
          <div className="kv-row" key={f.key}>
            <dt>{f.label}</dt>
            <dd>{showSetting(f.kind, f.value, f.kind === "money" ? d.po.currency : null)}{" "}
              {f.source === "overridden" ? <Chip tone="info">custom</Chip> : <Chip tone="muted">default</Chip>}
              {f.looser && <> <LooserMark /></>}
            </dd>
          </div>
        ))}
        <div className="kv-row">
          <dt>Rules switched off</dt>
          <dd>{off.length === 0 ? "none" : off.map((r) => r.name).join(", ")}
            {d.rules.some((r) => r.looser) && <> <LooserMark title="A rule that is on by default is switched off for this PO" /></>}
          </dd>
        </div>
      </dl>
    </Section>
  );
}
