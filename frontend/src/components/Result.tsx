import { useMemo, useState } from "react";
import { Chip, Confidence, DetailList, Disclosure, Section } from "./common";
import { PageViewer, type PageTarget } from "./PageViewer";
import { plainReason, reviewReasons, type PlainReason } from "../reasons";
import { DECISION, FIELD_LABEL, GROUNDING, HEADER_FIELDS, MONEY_FIELDS, OUTCOME, humanize, money, score, usd } from "../format";
import { linkProps } from "../router";
import type { Decision, EvidencedField, RuleRow, RunView, SettingsUsed } from "../types";

// ------------------------------------------------------------------------------------------------ reasons

// A plain sentence; rule id, outcome code, severity and cited facts only behind "Technical details".
export function ReasonItem({ reason }: { reason: PlainReason }) {
  const [open, setOpen] = useState(false);
  const t = reason.technical;
  return (
    <li className="reason">
      <span>{reason.text}</span>
      {t && (
        <>
          {" "}
          <button type="button" className="tech-btn" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? "Hide technical details" : "Technical details"}
          </button>
          {open && (
            <dl className="tech">
              {t.ruleId && <div><dt>Check</dt><dd><code>{t.ruleId}</code>{t.ruleName && <> · {t.ruleName}</>}</dd></div>}
              {t.floorCode && <div><dt>Floor reason</dt><dd><code>{t.floorCode}</code></dd></div>}
              {t.outcome && <div><dt>Outcome</dt><dd><code>{t.outcome}</code></dd></div>}
              {t.severity !== undefined && <div><dt>Severity</dt><dd>{t.severity}</dd></div>}
              {t.facts.length > 0 && <div><dt>Audit facts</dt><dd><code>{t.facts.join(" ")}</code></dd></div>}
            </dl>
          )}
        </>
      )}
    </li>
  );
}

// ------------------------------------------------------------------------------------------------ decision

export function DecisionBanner({ view }: { view: RunView }) {
  const d = view.decision;
  if (!d) {
    return (
      <div className="banner banner-failed" role="status">
        <div className="banner-icon" aria-hidden="true">✕</div>
        <div>
          <div className="banner-kicker">Run failed</div>
          <div className="banner-title">No decision</div>
          <p className="banner-text">
            {view.error ? `${view.error.message} Nothing was committed; the run was rolled back.` : "The run did not finish."}
          </p>
        </div>
      </div>
    );
  }
  const meta = DECISION[d];
  const ex = view.explanation;
  return (
    <div className={`banner banner-${meta.tone}`} role="status">
      <div className="banner-icon" aria-hidden="true">{meta.icon}</div>
      <div className="banner-main">
        <div className="banner-kicker">Decision</div>
        <div className="banner-title">{meta.label}</div>
        <p className="banner-text">{meta.meaning}</p>
        {view.actions.escalated && (
          <p className="banner-note">Approval was withheld at the last step: {String(view.actions.escalated.reason)}.</p>
        )}
      </div>
      {ex && (
        <div className="banner-why">
          <h3>Why</h3>
          <ul className="reasons">
            {ex.reasons.map((r, i) => <ReasonItem key={i} reason={plainReason(r.text, r.facts)} />)}
          </ul>
          <p className="next"><strong>Next step:</strong> {ex.next_step}</p>
          <p className="source" title={ex.fallback_reason ?? undefined}>
            {ex.source === "llm" ? `Written by ${ex.model} from the audit trail, checked against it.` :
              `Written from the audit trail by a template${ex.fallback_reason ? ` (${ex.fallback_reason.split(":")[0].replace(/_/g, " ")})` : ""}.`}
          </p>
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ fields

function fieldValue(name: string, f: EvidencedField, currency: string | null): string {
  if (f.value === null || f.value === undefined || f.value === "") return "—";
  if (MONEY_FIELDS.has(name)) return money(f.value, currency);
  return String(f.value);
}

export function FieldsTable({ view, onOpenPage }: { view: RunView; onOpenPage: (t: PageTarget) => void }) {
  const ex = view.extracted;
  if (!ex) return <p className="dim">Nothing was extracted.</p>;
  const currency = ((ex.currency as EvidencedField | undefined)?.value as string | null) ?? null;
  return (
    <>
      <div className="table-wrap">
        <table className="table fields">
          <thead>
            <tr><th>Field</th><th>Value</th><th>Confidence</th><th>Evidence</th><th>Source text</th></tr>
          </thead>
          <tbody>
            {HEADER_FIELDS.map((name) => {
              const f = ex[name] as EvidencedField | undefined;
              if (!f) return null;
              const missing = f.value === null || f.value === undefined || f.value === "";
              const g = f.grounding ? GROUNDING[f.grounding] : null;
              return (
                <tr key={name} className={missing ? "row-missing" : undefined}>
                  <th scope="row">
                    {FIELD_LABEL[name]}
                    {name === "po_reference" && f.explicit === false && <span className="tag">inferred</span>}
                    {name === "tax" && f.included_in_total === true && <span className="tag">included in total</span>}
                  </th>
                  <td className={MONEY_FIELDS.has(name) ? "num" : undefined}>{missing ? <span className="dim">not found</span> : fieldValue(name, f, currency)}</td>
                  <td>{missing ? <span className="dim">—</span> : <Confidence value={f.confidence} model={f.model_confidence} />}</td>
                  <td>{g ? <Chip tone={g.tone} title={g.help}>{g.label}</Chip> : <span className="dim">—</span>}</td>
                  <td className="src">
                    {f.source_text ? (
                      f.page ? (
                        <button type="button" className="src-link" onClick={() => onOpenPage({ page: f.page!, label: FIELD_LABEL[name], sourceText: f.source_text })}
                                title="Show the page">
                          <span className="page-tag">p.{f.page}</span> <q>{f.source_text}</q>
                        </button>
                      ) : <q>{f.source_text}</q>
                    ) : <span className="dim">—</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <h3 className="sub">Line items</h3>
      {ex.line_items.length === 0 ? <p className="dim">No line items.</p> : (
        <div className="table-wrap">
          <table className="table lines">
            <thead><tr><th>#</th><th>Description</th><th className="num">Qty</th><th className="num">Unit price</th><th className="num">Amount</th><th>Confidence</th><th>Page</th></tr></thead>
            <tbody>
              {ex.line_items.map((l, i) => (
                <tr key={i}>
                  <td className="dim">{i + 1}</td>
                  <td>{l.description ?? <span className="dim">—</span>}{l.item_code && <span className="tag">{l.item_code}</span>}</td>
                  <td className="num">{l.quantity ?? "—"}</td>
                  <td className="num">{money(l.unit_price)}</td>
                  <td className="num">{money(l.amount)}</td>
                  <td><Confidence value={l.confidence} model={l.model_confidence} /></td>
                  <td>{l.page ? <button type="button" className="src-link" onClick={() => onOpenPage({ page: l.page!, label: `Line ${i + 1}`, sourceText: l.source_text })}><span className="page-tag">p.{l.page}</span></button> : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {ex.adjustments.length > 0 && (
        <>
          <h3 className="sub">Adjustments</h3>
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Kind</th><th>Description</th><th className="num">Amount (signed)</th><th>Source text</th></tr></thead>
              <tbody>
                {ex.adjustments.map((a, i) => (
                  <tr key={i}>
                    <td>{humanize(a.kind ?? "other")}</td>
                    <td>{a.description ?? <span className="dim">—</span>}</td>
                    <td className="num">{money(a.amount)}</td>
                    <td className="src">{a.source_text ? <q>{a.source_text}</q> : <span className="dim">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {(ex.extraction_notes || ex.document_quality?.issues?.length > 0) && (
        <Disclosure summary={<span>Extraction notes{ex.document_quality?.type ? ` · ${ex.document_quality.type} document` : ""}</span>} className="notes">
          {ex.document_quality?.issues?.length > 0 && <p><strong>Document issues:</strong> {ex.document_quality.issues.join(", ")}</p>}
          {ex.extraction_notes && <p className="pre">{ex.extraction_notes}</p>}
        </Disclosure>
      )}
    </>
  );
}

// ------------------------------------------------------------------------------------------------ rules

const OUTCOME_ORDER = { fail: 0, flag: 1, info: 2, pass: 3 } as const;

export function RulesTable({ rules }: { rules: RuleRow[] }) {
  const sorted = useMemo(() => [...rules].sort((a, b) => OUTCOME_ORDER[a.outcome] - OUTCOME_ORDER[b.outcome] || b.severity - a.severity),
                         [rules]);
  const triggered = rules.filter((r) => r.outcome === "flag" || r.outcome === "fail").length;
  return (
    <>
      <p className="lede">{triggered === 0 ? "No rule was triggered." : `${triggered} of ${rules.length} results triggered.`} The decision is the most severe outcome; nothing can lower it.</p>
      <ul className="rules">
        {sorted.map((r) => (
          <li key={r.rule_id} className={`rule rule-${r.outcome}`}>
            <Disclosure summary={
              <span className="rule-head">
                <Chip tone={OUTCOME[r.outcome].tone}>{OUTCOME[r.outcome].label}</Chip>
                <span className="rule-name">{r.name}{r.kind === "floor" && <span className="tag">engine floor</span>}</span>
                <span className="rule-msg">{r.message}</span>
                {r.severity > 0 && <span className="sev" title="Severity (1 review, 2 request info, 3 reject)">sev {r.severity}</span>}
              </span>
            }>
              <div className="rule-detail">
                <p className="dim"><code>{r.rule_id}</code>{r.outcome_key && <> · outcome <code>{r.outcome_key}</code></>}</p>
                <DetailList data={r.detail} hide={["severity", "outcome_key"]} />
              </div>
            </Disclosure>
          </li>
        ))}
      </ul>
    </>
  );
}

// ------------------------------------------------------------------------------------------------ match

const SIGNALS: [string, string, number][] = [["reference", "Reference", 0.4], ["vendor", "Vendor", 0.25], ["amount", "Amount", 0.2], ["lines", "Lines", 0.15]];

export function MatchPanel({ view }: { view: RunView }) {
  const v = view.vendor;
  const m = view.match;
  return (
    <div className="match">
      <div className="match-vendor">
        <div className="label">Vendor</div>
        {v?.record ? (
          <>
            <div className="big">{v.record.name}</div>
            <div className="dim">
              {humanize(String(v.method))} match, score {score(v.score as number)} · status {v.record.status}
              {v.record.tax_id && <> · tax ID {v.record.tax_id}</>}
            </div>
          </>
        ) : <div className="big dim">Not resolved</div>}
      </div>
      <div className="match-po">
        <div className="label">Purchase order</div>
        <div className="big">{m.matched_po ?? <span className="dim">No confident match</span>}</div>
        <div className="dim">{m.message}</div>
      </div>
      {m.candidates.length > 0 && (
        <div className="table-wrap">
          <table className="table candidates">
            <thead><tr><th>PO</th><th className="num">Score</th>{SIGNALS.map(([, l, w]) => <th key={l}>{l} <span className="dim">({w})</span></th>)}</tr></thead>
            <tbody>
              {m.candidates.map((c) => (
                <tr key={c.po_number} className={c.po_number === m.matched_po ? "row-match" : undefined}>
                  <th scope="row">{c.po_number}{c.po_number === m.matched_po && <span className="tag tag-accent">matched</span>}</th>
                  <td className="num">{score(c.score)}</td>
                  {SIGNALS.map(([k, , w]) => (
                    <td key={k}>
                      <span className="sig" title={(c.reasons.find((r) => r.startsWith(k)) ?? "").replace(`${k}:`, "")}>
                        <span className="sig-bar"><span style={{ width: `${Math.min(100, ((c.breakdown[k] ?? 0) / w) * 100)}%` }} /></span>
                        <span className="num">{(c.breakdown[k] ?? 0).toFixed(2)}</span>
                      </span>
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ actions

export function ActionsPanel({ view }: { view: RunView }) {
  const a = view.actions;
  const inv = view.invoice;
  const cur = (inv?.currency as string | null) ?? null;
  return (
    <div className="actions">
      {inv && (
        <div className="action">
          <div className="label">Invoice recorded</div>
          <div>Status <strong>{humanize(String(inv.status))}</strong> · total {money(inv.total, cur)}</div>
        </div>
      )}
      {a.ledger && (
        <div className="action action-ledger">
          <div className="label">Ledger commit</div>
          <div><strong>{money(a.ledger.amount, cur)}</strong> committed to <strong>{String(a.ledger.po_number)}</strong></div>
          <div className="balance">
            Balance <span className="num">{money(a.ledger.balance_before, cur)}</span> <span aria-hidden="true">→</span><span className="sr">to</span> <span className="num">{money(a.ledger.balance_after, cur)}</span>
            {a.ledger.over_balance === true && <Chip tone="flag">over-billed</Chip>}
          </div>
          {a.ready_for_payment && <Chip tone="pass">Ready for payment</Chip>}
        </div>
      )}
      {a.review.map((r) => (
        <div className="action" key={r.id}>
          <div className="label">Review queue</div>
          <div>Item #{r.id} · <strong>{r.status}</strong></div>
          <ul className="reasons small">
            {reviewReasons(r.reason).map((p, i) => <ReasonItem key={i} reason={p} />)}
          </ul>
        </div>
      ))}
      {a.drafts.map((d) => (
        <div className="action draft" key={d.id}>
          <div className="label">{d.kind === "vendor_email" ? "Vendor email (draft)" : "Internal notification (draft)"}</div>
          <p className="notice">Nothing is sent. Drafts are reviewed and sent by a person.</p>
          <div className="mail">
            <div className="mail-row"><span className="dim">To</span> {d.to ?? <span className="dim">(no vendor contact on file)</span>}</div>
            <div className="mail-row"><span className="dim">Subject</span> {d.subject}</div>
            <pre className="mail-body">{d.body}</pre>
          </div>
        </div>
      ))}
      {!a.ledger && a.review.length === 0 && a.drafts.length === 0 && <p className="dim">No further action was recorded.</p>}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ cost

export function CostPanel({ view }: { view: RunView }) {
  const r = view.run;
  return (
    <dl className="cost">
      <div><dt>Total</dt><dd className="num">{usd(r.cost_usd ?? 0)}</dd></div>
      {Object.entries(view.stage_costs).map(([k, v]) => <div key={k}><dt>{humanize(k)}</dt><dd className="num">{usd(v)}</dd></div>)}
      <div><dt>Tokens in / out</dt><dd className="num">{r.tokens_in ?? 0} / {r.tokens_out ?? 0}</dd></div>
      <div><dt>Model</dt><dd>{r.model ?? "—"}</dd></div>
    </dl>
  );
}

// ------------------------------------------------------------------------------------------------ the whole result

// Which settings judged this run: the matched PO's effective settings, or the global defaults (SPEC section 11 item 93).
export function SettingsUsedPanel({ used }: { used: SettingsUsed }) {
  const overridden = Object.entries(used.sources).filter(([, s]) => s === "override").map(([k]) => k);
  const off = Object.entries(used.rules_enabled).filter(([, on]) => !on).map(([id]) => id);
  return (
    <div className="settings-used-panel">
      <p>{used.message}</p>
      <dl className="kv">
        {Object.entries(used.values ?? {}).map(([k, v]) => (
          <div className="kv-row" key={k}>
            <dt><code>{k}</code></dt>
            <dd>{String(v)} {overridden.includes(k) ? <Chip tone="info">this PO</Chip> : <Chip tone="muted">default</Chip>}</dd>
          </div>
        ))}
        <div className="kv-row"><dt>Rules switched off</dt><dd>{off.length ? off.map((id) => <code key={id}>{id} </code>) : "none"}</dd></div>
      </dl>
      {used.scope === "po" && used.po_id !== null && <p className="dim small"><a {...linkProps(`/settings/pos/${used.po_id}`)}>Rules for {used.po_number}</a></p>}
    </div>
  );
}

export function ResultView({ view }: { view: RunView }) {
  const [target, setTarget] = useState<PageTarget | null>(null);
  const decision = view.decision as Decision | null;
  return (
    <div className="result">
      <DecisionBanner view={view} />
      {view.extracted && (
        <Section title="Extracted fields" id="fields"
                 aside={view.pages.length > 0 && <button type="button" className="btn-ghost" onClick={() => setTarget({ page: view.pages[0], label: "Invoice", sourceText: null })}>View invoice</button>}>
          <FieldsTable view={view} onOpenPage={setTarget} />
        </Section>
      )}
      {view.rules.length > 0 && <Section title="Checks" id="rules"><RulesTable rules={view.rules} /></Section>}
      {view.settings_used && <Section title="Settings used" id="settings-used"><SettingsUsedPanel used={view.settings_used} /></Section>}
      {view.vendor && <Section title="Vendor and purchase order" id="match"><MatchPanel view={view} /></Section>}
      {decision && <Section title="What was written" id="actions"><ActionsPanel view={view} /></Section>}
      <Section title="Cost" id="cost"><CostPanel view={view} /></Section>
      {target && <PageViewer runId={view.run.id} target={target} pages={view.pages} onClose={() => setTarget(null)}
                             onPage={(n) => setTarget({ ...target, page: n })} />}
    </div>
  );
}
