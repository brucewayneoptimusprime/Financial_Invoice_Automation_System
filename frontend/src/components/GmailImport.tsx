import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ApiError, gmailConnectStart, gmailDisconnect, gmailImport, gmailLabels, gmailSearch, gmailStatus, type GmailSearchBody } from "../api";
import { usd, when } from "../format";
import { linkProps } from "../router";
import type { GmailImportOutcome, GmailLabel, GmailMessage, GmailSearchResult, GmailStatus } from "../types";
import { Chip } from "./common";
import gmailIcon from "../assets/gmail-icon.png";

// Gmail import (read-only). Search -> the person ticks attachments -> import. Nothing is pre-ticked, nothing is imported on its own,
// and every text that came from an email (sender, subject, snippet, file names) is shown as plain text, never as HTML.

const RETURN_MESSAGES: Record<string, string> = {
  state_invalid: "The connection could not be confirmed (the sign-in link was not one this page started). Try again.",
  state_expired: "The sign-in took too long and expired. Try again.",
  binding_mismatch: "The sign-in came back without this browser's connection check (a cookie from the start of the sign-in). Start again from this page, in the same browser, with cookies allowed for this site.",
  denied: "Gmail access was not granted.",
  exchange_failed: "Google did not complete the connection. Try again.",
  scope_mismatch: "Google granted a different permission than read-only Gmail, so the connection was refused.",
  no_refresh_token: "Google did not return a lasting connection. Remove this app's access in your Google account, then try again.",
  not_set_up: "Gmail import is not set up on the server.",
};

const key = (messageId: string, partId: string) => `${messageId}|${partId}`;

const LABEL_VIEW: Record<GmailLabel["label"], { text: string; tone: string }> = {
  likely_invoice: { text: "likely invoice", tone: "pass" },
  unsure: { text: "unsure", tone: "muted" },
  unlikely: { text: "unlikely", tone: "flag" },
};

function size(bytes: number): string {
  return bytes >= 1_048_576 ? `${(bytes / 1_048_576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function readReturn(): { tone: "pass" | "fail"; text: string } | null {
  const params = new URLSearchParams(window.location.search);
  const result = params.get("gmail");
  if (!result) return null;
  const code = params.get("code") ?? "";
  params.delete("gmail");
  params.delete("code");
  const rest = params.toString();
  window.history.replaceState(null, "", `${window.location.pathname}${rest ? `?${rest}` : ""}`);
  if (result === "connected") return { tone: "pass", text: "Gmail is connected (read-only)." };
  return { tone: "fail", text: RETURN_MESSAGES[code] ?? "The Gmail connection did not complete. Try again." };
}

export interface GmailImportProps {
  onImported(outcomes: GmailImportOutcome[]): void;
  hostname?: string;                                    // injectable for tests
  navigateTo?: (url: string) => void;                   // injectable for tests (the real one leaves the page for Google)
}

export function GmailImport({ onImported, hostname = window.location.hostname, navigateTo = (u) => window.location.assign(u) }: GmailImportProps) {
  const [status, setStatus] = useState<GmailStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [banner] = useState(readReturn);
  const [query, setQuery] = useState("");
  const [sentence, setSentence] = useState("");
  const [lastSearch, setLastSearch] = useState<GmailSearchBody | null>(null);
  const [translated, setTranslated] = useState<{ query: string; notes: string } | null>(null);
  const [translateCost, setTranslateCost] = useState<string | null>(null);
  const [labels, setLabels] = useState<Map<string, GmailLabel>>(new Map());
  const [labelsState, setLabelsState] = useState<"idle" | "loading" | "done" | "unavailable">("idle");
  const [labelsCost, setLabelsCost] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);
  const [queryOpen, setQueryOpen] = useState(false);          // the editable Gmail query: collapsed unless needed or asked for
  const [problems, setProblems] = useState<string[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [result, setResult] = useState<GmailSearchResult | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [importing, setImporting] = useState(false);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const onIp = hostname === "127.0.0.1";

  const loadStatus = useCallback(() => {
    gmailStatus().then((s) => {
      if (!s || typeof s.backend !== "string" || !s.caps) {              // an older or unexpected server: never break the upload screen
        setStatus(null);
        setStatusError("Gmail import is not available from this server.");
        return;
      }
      setStatus(s);
      setStatusError(null);
    })
      .catch((e) => setStatusError(e instanceof ApiError ? e.message : "The Gmail status could not be loaded."));
  }, []);
  useEffect(loadStatus, [loadStatus]);

  // Labels are asked for ONCE per search, after the results are on screen; they only add a hint next to each checkbox.
  const fetchLabels = useCallback(async (res: GmailSearchResult) => {
    const anyImportable = res.messages.some((m) => m.attachments.some((a) => a.eligible));
    if (!status?.labels_available || !anyImportable) return;   // nothing to label: no call (the server would not call the model either)
    setLabelsState("loading");
    try {
      const r = await gmailLabels(res.search_id);
      if (!r.ok) { setLabelsState("unavailable"); return; }
      setLabels(new Map(r.body.labels.map((l) => [key(l.message_id, l.part_id), l])));
      setLabelsCost(r.body.cost.tokens_in > 0 ? r.body.cost.labels_usd : null);
      setLabelsState(r.body.fallback ? "unavailable" : "done");
    } catch {
      setLabelsState("unavailable");
    }
  }, [status?.labels_available]);

  const runSearch = useCallback(async (body: GmailSearchBody, keepPicks = false, refresh = false) => {
    setSearching(true);
    setProblems([]);
    setMessage(null);
    const fromSentence = "sentence" in body;
    if (fromSentence) { setTranslated(null); setTranslateCost(null); }
    try {
      const r = await gmailSearch(body);
      setLastSearch(body);
      if (r.ok) {
        setResult(r.body);
        if (!keepPicks) setPicked(new Set());
        if (!refresh) {                                       // a refresh after an import keeps the labels it had: no second labels call
          setLabels(new Map());
          setLabelsCost(null);
          setLabelsState("idle");
          void fetchLabels(r.body);
        }
        if (fromSentence && r.body.translation) {
          setQuery(r.body.translation.query);                 // Claude's query lands in the manual box, still editable
          setTranslated({ query: r.body.translation.query, notes: r.body.translation.notes });
          setTranslateCost(r.body.cost?.translate_usd ?? null);
        } else if (!fromSentence) {
          setTranslated(null);
          setTranslateCost(null);
        }
      } else {
        setResult(null);
        setPicked(new Set());
        setMessage(r.body.message ?? `The search failed (${r.status}).`);
        setProblems(r.body.problems ?? []);
        if (fromSentence && r.body.error === "translation_failed") {
          setQueryOpen(true);                                 // the manual query is now the way forward: show it
          if (r.body.query) setQuery(r.body.query);           // the refused query, to fix by hand
          setTranslateCost(r.body.cost?.translate_usd ?? null);
        }
        if (r.body.error === "not_connected" || r.body.error === "reconnect") loadStatus();
        setLabels(new Map());
        setLabelsState("idle");
        setLabelsCost(null);
      }
    } catch {
      setMessage("The search failed. Is the API running?");
    } finally {
      setSearching(false);
    }
  }, [loadStatus, fetchLabels]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!searching) runSearch({ query });
  };
  const find = (e: FormEvent) => {
    e.preventDefault();
    if (!searching && sentence.trim()) runSearch({ sentence });
  };

  const max = status?.caps.max_import ?? 10;
  const toggle = (id: string) => setPicked((p) => {
    const next = new Set(p);
    if (next.has(id)) next.delete(id);
    else if (next.size < max) next.add(id);
    return next;
  });

  const doImport = async () => {
    if (!result || picked.size === 0 || importing) return;
    setImporting(true);
    setMessage(null);
    setProblems([]);
    const items = [...picked].map((k) => { const [message_id, part_id] = k.split("|"); return { message_id, part_id }; });
    try {
      const r = await gmailImport(result.search_id, items);
      if (r.ok) {
        onImported(r.body.items);
        setPicked(new Set());
        loadStatus();
        await runSearch({ query: result.query_sent }, false, true);   // refresh (no model call): imported attachments now link to their runs
      } else {
        setMessage(r.body.message ?? `The import failed (${r.status}).`);
        setProblems(r.body.problems ?? []);
      }
    } catch {
      setMessage("The import failed. Is the API running?");
    } finally {
      setImporting(false);
    }
  };

  const connect = async () => {
    setMessage(null);
    try {
      const { authorization_url } = await gmailConnectStart();
      navigateTo(authorization_url);
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "Could not start the Gmail connection.");
    }
  };

  const disconnect = async () => {
    setConfirmDisconnect(false);
    const r = await gmailDisconnect().catch(() => null);
    setResult(null);
    setPicked(new Set());
    setMessage(r && r.ok ? (r.body.revoked ? "Gmail disconnected." : "Gmail disconnected here; Google did not confirm the revocation.")
                         : "The disconnect failed.");
    loadStatus();
  };

  return (
    <section className="gmail-panel" aria-labelledby="gmail-h">
      <div className="gmail-head">
        <h2 id="gmail-h"><img src={gmailIcon} alt="" className="gmail-logo" width={24} height={24} />Import from Gmail</h2>
        {status?.fake && <Chip tone="flag" title="GMAIL_BACKEND=fake: labelled test data, Google is never contacted">FAKE INBOX (test data)</Chip>}
        {status?.connected && <span className="dim small">{status.account_email} · read-only</span>}
        {status?.connected && !status.fake && (confirmDisconnect ? (
          <span className="gmail-confirm">
            <span>Disconnect this account?</span>
            <button type="button" className="btn-ghost small-btn" onClick={disconnect}>Disconnect</button>
            <button type="button" className="btn-ghost small-btn" onClick={() => setConfirmDisconnect(false)}>Cancel</button>
          </span>
        ) : (
          <button type="button" className="btn-ghost small-btn" onClick={() => setConfirmDisconnect(true)}>Disconnect</button>
        ))}
      </div>

      {onIp && (
        <p className="warn" role="alert">
          Open this page at <a href="http://localhost:5173/invoices">http://localhost:5173</a> to use Gmail import: Google returns you to
          localhost, and the connection check only works there, not on 127.0.0.1.
        </p>
      )}
      {banner && <p className={banner.tone === "pass" ? "gmail-ok" : "error"} role="status">{banner.text}</p>}
      {statusError && <p className="error">{statusError}</p>}

      {status === null && !statusError && <p className="dim">Checking Gmail…</p>}

      {status && !status.available && (
        <p className="dim">
          Gmail import is not set up{status.missing.length > 0 ? ` (missing ${status.missing.join(", ")})` : " (turned off)"}. Uploading files
          below works as usual.
        </p>
      )}

      {status?.available && !status.connected && (
        <div className="gmail-connect">
          <p>{status.reconnect ? "The Gmail connection can no longer be used. Connect again." :
            "Connect a Gmail account to pick invoice attachments from it. Access is read-only: nothing is sent, changed or deleted."}</p>
          <button type="button" className="btn" onClick={connect} disabled={onIp}>Connect Gmail (read-only)</button>
        </div>
      )}

      {status?.connected && (
        <>
          {status.translator_available && (
            <form className="gmail-search" onSubmit={find}>
              <label htmlFor="gmail-sentence">Describe what you're looking for</label>
              <div className="gmail-search-row">
                <input id="gmail-sentence" type="text" value={sentence} maxLength={status.caps.request_max_chars}
                       placeholder="invoices from Meridian since August" onChange={(e) => setSentence(e.target.value)} autoComplete="off" />
                <button type="submit" className="btn" disabled={searching || !sentence.trim()}>{searching && lastSearch && "sentence" in lastSearch ? "Finding…" : "Find"}</button>
              </div>
              <p className="dim small" data-testid="gmail-sentence-hint">
                Claude turns this sentence into a Gmail search.
                {status.labels_available && " Afterwards it labels the results using each email's sender, subject, snippet and attachment names, never the full email or the PDFs."}
              </p>
            </form>
          )}
          {translated && (
            <p className="gmail-translated small" role="status">
              Claude wrote this search; open "Edit search query" to change it.{translated.notes ? ` ${translated.notes}` : ""}
            </p>
          )}
          {status.translator_available && (
            <button type="button" className="gmail-toggle" aria-expanded={queryOpen} aria-controls="gmail-query-box"
                    onClick={() => setQueryOpen((open) => !open)}>
              <span className="gmail-caret" aria-hidden="true">▾</span> Edit search query
            </button>
          )}
          {(queryOpen || !status.translator_available) && (       // without a model the query box is the only way to search
          <form className="gmail-search" onSubmit={submit} role="search" id="gmail-query-box">
            <label htmlFor="gmail-q">Gmail search</label>
            <div className="gmail-search-row">
              <input id="gmail-q" type="search" value={query} maxLength={status.caps.query_max_chars} placeholder='from:billing@acme.com after:2026/08/01'
                     onChange={(e) => setQuery(e.target.value)} autoComplete="off" spellCheck={false} />
              <button type="submit" className="btn" disabled={searching}>{searching ? "Searching…" : result ? "Search again" : "Search"}</button>
            </div>
            <p className="dim small">
              Words, "phrases", OR, -word, and from:, to:, subject:, after:, before:, newer_than:, older_than:, filename:, has:attachment.
              Only emails with attachments from the last {status.caps.default_window_days} days are searched unless you give a date.
            </p>
          </form>
          )}

          {message && (
            <div className="error" role="alert">
              <p>{message}</p>
              {problems.length > 1 && <ul>{problems.map((p) => <li key={p}>{p}</li>)}</ul>}
              {problems.length === 1 && lastSearch && "sentence" in lastSearch && <p>{problems[0]}</p>}
              {!result && translateCost !== null && <p className="small">Cost of the attempt: {usd(translateCost)}.</p>}
            </div>
          )}

          {result && (
            <div className="gmail-results">
              <p className="dim small">
                Sent to Gmail: <code>{result.query_sent}</code>
                {result.added_terms.length > 0 && <> (added: {result.added_terms.join(" ")})</>}
              </p>
              <CostLine translate={translateCost} labels={labelsCost} />
              {labelsState === "loading" && <p className="dim small" role="status">Labelling the attachments…</p>}
              {labelsState === "done" && labels.size > 0 && (
                <p className="dim small">Labels are hints from Claude, read from the email's details only. Nothing is ticked for you.</p>
              )}
              {labelsState === "unavailable" && <p className="dim small">No labels for this search (Claude was not available).</p>}
              {result.truncated && (
                <p className="dim small">Showing the newest {result.messages.length} of about {result.result_estimate}. Narrow the search to see others.</p>
              )}
              {result.messages.length === 0 ? <p className="dim">No email with attachments matches.</p> : (
                <ul className="gmail-list">
                  {result.messages.map((m) => <MessageCard key={m.message_id} m={m} picked={picked} full={picked.size >= max} onToggle={toggle}
                                                           labels={labels} />)}
                </ul>
              )}
              <div className="gmail-foot">
                <button type="button" className="btn" disabled={picked.size === 0 || importing} onClick={doImport}>
                  {importing ? "Importing…" : `Import ${picked.size} selected`}
                </button>
                <span className="dim small">
                  At most {max} per import.{picked.size >= max ? " Limit reached." : ""}
                  {status.budget_remaining_usd !== null && <> Budget left this session: {usd(status.budget_remaining_usd)}.</>}
                </span>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function CostLine({ translate, labels }: { translate: string | null; labels: string | null }) {
  if (translate === null && labels === null) return null;
  const text = translate !== null && labels !== null
    ? `${usd(translate)} (query) + ${usd(labels)} (labels) = ${usd(Number(translate) + Number(labels))}, by Claude`
    : translate !== null ? `${usd(translate)} (query by Claude)` : `${usd(labels)} (labels by Claude)`;
  return <p className="dim small" data-testid="gmail-cost">This search: {text}.</p>;
}

function MessageCard({ m, picked, full, onToggle, labels }: { m: GmailMessage; picked: Set<string>; full: boolean; onToggle(id: string): void;
                                                              labels: Map<string, GmailLabel> }) {
  return (
    <li className="gmail-msg">
      <div className="gmail-msg-head">
        <span className="gmail-from">{m.sender ?? "(no sender)"}</span>
        <span className="dim small">{m.date ? when(m.date) : ""}</span>
      </div>
      <div className="gmail-subject">{m.subject ?? "(no subject)"}</div>
      {m.snippet && <p className="gmail-snippet">{m.snippet}</p>}
      {m.reader_instructions && (
        <p className="gmail-flag">
          <Chip tone="flag">Text addressed to an AI</Chip> in the {m.reader_instruction_fields.join(", ")}. It is treated as data and
          changes nothing; check the email before importing.
        </p>
      )}
      <ul className="gmail-atts">
        {m.attachments.map((a) => {
          const id = key(m.message_id, a.part_id);
          const checked = picked.has(id);
          const disabled = !a.eligible || !!a.imported_run_id || (full && !checked);
          const hint = a.eligible ? labels.get(id) : undefined;      // a hint only: it changes nothing about the checkbox
          return (
            <li key={a.part_id} className={a.eligible ? "" : "gmail-att-off"}>
              <label>
                <input type="checkbox" checked={checked} disabled={disabled} onChange={() => onToggle(id)}
                       aria-label={`Import ${a.filename}`} />
                <span className="gmail-file">{a.filename}</span>
                <span className="dim small">{a.mime_type} · {size(a.size_bytes)}{a.inline ? " · inline image" : ""}</span>
              </label>
              {hint && (
                <span className="gmail-hint" data-testid={`label-${id}`}>
                  <Chip tone={LABEL_VIEW[hint.label].tone} title={hint.source === "rule" ? "Set by a rule, not by Claude" : "A hint from Claude"}>
                    {LABEL_VIEW[hint.label].text}
                  </Chip>
                  <span className="dim small">{hint.reason}</span>
                </span>
              )}
              {a.imported_run_id ? <a {...linkProps(`/runs/${a.imported_run_id}`)} className="small">imported, see run</a>
                : !a.eligible && <span className="dim small">{a.reason}</span>}
            </li>
          );
        })}
        {m.more_attachments > 0 && <li className="dim small">{m.more_attachments} more attachment(s) not shown</li>}
      </ul>
    </li>
  );
}
