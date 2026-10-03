import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { App } from "../App";
import { parse } from "../router";
import { SettingsScreen } from "../screens/Settings";
import { POSettingsScreen } from "../screens/POSettings";
import { PODetailScreen } from "../screens/PODetail";
import { ResultView } from "../components/Result";
import { StageTimeline } from "../components/StageTimeline";
import { initialRunState, liveRules, runReducer, type RunState } from "../runState";
import { checkSetting } from "../components/settingsFormat";
import type { AuditEvent, GlobalSettings, POSettings, RunView } from "../types";
import { auditOf, STREAMS, VIEWS } from "./fixtures";
import globalFx from "./fixtures/settings_global.json";
import posFx from "./fixtures/settings_pos.json";
import poCustom from "./fixtures/settings_po_custom.json";
import poDefault from "./fixtures/settings_po_default.json";
import historyFx from "./fixtures/settings_history.json";
import runViewFx from "./fixtures/settings_run_view.json";
import matched from "./fixtures/po_detail_matched.json";

// Settings (SETTINGS_PLAN, stage S3): the gear, global defaults, the PO list and editor, "looser than default", "Rules for this PO",
// and the settings_applied event ("Settings used") in the run timeline and the run view. Fixtures come from the real endpoints.

const G = globalFx as unknown as GlobalSettings;
const CUSTOM = poCustom as unknown as POSettings;
let posts: { url: string; body: Record<string, unknown> }[] = [];

function mockFetch(over: Record<string, unknown> = {}) {
  posts = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    if (init?.method === "POST") posts.push({ url, body: JSON.parse(String(init.body ?? "{}")) });
    for (const [prefix, body] of Object.entries(over)) if (url.startsWith(prefix)) return reply(body);
    if (url.startsWith("/api/settings/history")) return reply(historyFx);
    if (url.startsWith("/api/settings/global")) return reply({ ...G, changed: 1 });
    if (url.startsWith("/api/settings/pos/1")) return reply(poCustom);
    if (url.startsWith("/api/settings/pos/")) return reply(poDefault);
    if (url.startsWith("/api/settings/pos")) return reply(posFx);
    if (url.startsWith("/api/settings")) return reply(G);
    if (url.startsWith("/api/pos/")) return reply(matched);
    if (url.startsWith("/api/health")) return reply({ status: "ok", mode: "replay", model: "claude-sonnet-5", session_spent_usd: "0",
                                                       session_ceiling_usd: "5.00", run_ceiling_usd: "0.25", queue_length: 0, max_file_bytes: 20971520 });
    if (url.startsWith("/api/review-queue")) return reply({ items: [], open_count: 0 });
    if (url.startsWith("/api/runs")) return reply({ runs: [] });
    if (url.startsWith("/api/dashboard")) return reply({});
    return reply({});
  }));
  vi.stubGlobal("EventSource", class { onopen = null; onerror = null; readyState = 0; static CLOSED = 2; addEventListener() {} close() {} });
}
afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState(null, "", "/"); });

describe("the gear and the routes", () => {
  it("parses /settings and /settings/pos/:id", () => {
    expect(parse("/settings")).toEqual({ name: "settings" });
    expect(parse("/settings/pos/7")).toEqual({ name: "settingsPO", id: 7 });
    expect(parse("/settings/pos/x")).toEqual({ name: "missing" });
  });
  it("shows the gear at the top right everywhere except the upload screen", async () => {
    mockFetch();
    for (const path of ["/pos", "/review", "/settings"]) {
      window.history.replaceState(null, "", path);
      const r = render(<App />);
      expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings");
      r.unmount();
    }
    window.history.replaceState(null, "", "/invoices");
    const r = render(<App />);
    expect(screen.queryByRole("link", { name: "Settings" })).toBeNull();
    r.unmount();
  });
});

describe("Settings: global defaults", () => {
  it("shows each value with its range, the locked rules and both floors switched on and disabled with a reason", async () => {
    mockFetch();
    render(<SettingsScreen />);
    expect(await screen.findByRole("heading", { name: "Settings" })).toBeInTheDocument();
    expect(screen.getByLabelText("Tolerance over the PO balance (percent)")).toHaveValue(2);
    expect(screen.getByText(/Range: 0 to 25 %/)).toBeInTheDocument();
    for (const id of ["r_duplicate_exact", "r_vendor_status"]) {
      const row = screen.getByText(id).closest("li")!;
      const box = within(row).getByRole("checkbox");
      expect(box).toBeChecked();
      expect(box).toBeDisabled();
      expect(row.textContent).toMatch(/\S{10,}/);
    }
    const arithmetic = screen.getByText("r_arithmetic").closest("li")!;
    expect(within(arithmetic).getByRole("checkbox")).not.toBeDisabled();
    for (const f of G.floors) {
      const row = screen.getByText(f.name).closest("li")!;
      expect(within(row).getByRole("checkbox")).toBeDisabled();
      expect(row).toHaveTextContent(f.reason);
    }
  });
  it("checks the range on the client and keeps Save disabled until the value is valid", async () => {
    mockFetch();
    render(<SettingsScreen />);
    const pct = await screen.findByLabelText("Tolerance over the PO balance (percent)");
    const save = screen.getByRole("button", { name: "Save global defaults" });
    expect(save).toBeDisabled();
    fireEvent.change(pct, { target: { value: "30" } });
    expect(screen.getByRole("alert")).toHaveTextContent("At most 25.");
    expect(save).toBeDisabled();
    fireEvent.change(pct, { target: { value: "3" } });
    expect(save).not.toBeDisabled();
    await act(async () => { fireEvent.click(save); });
    expect(posts).toEqual([{ url: "/api/settings/global", body: { values: { tolerance_pct: 3 }, rules: {} } }]);
    expect(await screen.findByRole("status")).toHaveTextContent("Saved 1 change");
  });
  it("shows the server's problems when it refuses", async () => {
    mockFetch();
    render(<SettingsScreen />);
    const days = await screen.findByLabelText(G.values.find((v) => v.key === "duplicate_days")!.label);
    vi.stubGlobal("fetch", vi.fn(async (url: string) => url.startsWith("/api/settings/global")
      ? new Response(JSON.stringify({ error: "invalid", message: "Some values are out of range.", problems: { duplicate_days: "At most 90." } }),
                     { status: 422, headers: { "Content-Type": "application/json" } })
      : new Response(JSON.stringify(historyFx), { status: 200, headers: { "Content-Type": "application/json" } })));
    fireEvent.change(days, { target: { value: "20" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Save global defaults" })); });
    expect(await screen.findByText("At most 90.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Some values are out of range.");
  });
  it("lists the POs as default or custom, with the looser marker on the looser one, and the recent changes with the actor", async () => {
    mockFetch();
    render(<SettingsScreen />);
    const link = await screen.findByRole("link", { name: /PO-SS-001/ });
    expect(link).toHaveAttribute("href", "/settings/pos/1");
    expect(within(link).getByText(/custom/)).toBeInTheDocument();
    expect(within(link).getByText("looser than default")).toBeInTheDocument();
    const other = screen.getByRole("link", { name: /PO-SS-002/ });
    expect(within(other).getByText("default")).toBeInTheDocument();
    expect(within(other).queryByText("looser than default")).toBeNull();
    expect(screen.getAllByText(/actor: unauthenticated demo user/).length).toBeGreaterThan(0);
  });
  it("searches the PO list through the API", async () => {
    mockFetch();
    render(<SettingsScreen />);
    await screen.findByRole("link", { name: /PO-SS-001/ });
    fireEvent.change(screen.getByRole("searchbox", { name: "Search purchase orders" }), { target: { value: "SS-002" } });
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([u]) => String(u).includes("q=SS-002"))).toBe(true));
  });
});

describe("the PO editor", () => {
  it("says which values inherit and which are overridden, with the looser marker on the looser ones", async () => {
    mockFetch();
    render(<POSettingsScreen id={1} />);
    expect(await screen.findByRole("heading", { name: "Rules for PO-SS-001" })).toBeInTheDocument();
    const pct = screen.getByTestId("po-setting-tolerance_pct");
    expect(pct).toHaveTextContent("overridden: 5.00%");
    expect(within(pct).getByText("looser than default")).toBeInTheDocument();
    const mode = screen.getByTestId("po-setting-tolerance_mode");
    expect(mode).toHaveTextContent("inherits default (Both limits");
    expect(within(mode).queryByText("looser than default")).toBeNull();
    const threshold = screen.getByTestId("po-setting-confidence_threshold");
    expect(threshold).toHaveTextContent("overridden: 0.90");
    expect(within(threshold).queryByText("looser than default")).toBeNull();        // stricter (higher), not looser
    const price = screen.getByTestId("po-rule-r_po_line_price");
    expect(within(price).getByRole("combobox")).toHaveValue("off");
    expect(within(price).getByText("looser than default")).toBeInTheDocument();
  });
  it("shows the locked rules without a switch, and the reason", async () => {
    mockFetch();
    render(<POSettingsScreen id={1} />);
    const locked = await screen.findByTestId("po-rule-r_duplicate_exact");
    expect(within(locked).queryByRole("combobox")).toBeNull();
    expect(locked).toHaveTextContent(CUSTOM.rules.find((r) => r.id === "r_duplicate_exact")!.reason!);
  });
  it("resets a value to the default by sending null, and a rule to inherit the same way", async () => {
    mockFetch();
    render(<POSettingsScreen id={1} />);
    const pct = await screen.findByTestId("po-setting-tolerance_pct");
    fireEvent.click(within(pct).getByRole("button", { name: "Reset to default" }));
    expect(pct).toHaveTextContent("will reset to the default (2.00%)");
    fireEvent.change(within(screen.getByTestId("po-rule-r_po_line_price")).getByRole("combobox"), { target: { value: "inherit" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Save rules for this PO" })); });
    expect(posts).toEqual([{ url: "/api/settings/pos/1", body: { values: { tolerance_pct: null }, rules: { r_po_line_price: null } } }]);
  });
  it("a PO with no overrides inherits everything and carries no looser marker", async () => {
    mockFetch();
    render(<POSettingsScreen id={2} />);
    await screen.findByRole("heading", { name: /Rules for PO-SS-002/ });
    expect(screen.queryByText("looser than default")).toBeNull();
    expect(screen.queryByText(/overridden:/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Reset to default" })).toBeNull();
  });
});

describe("Rules for this PO on the PO page", () => {
  it("shows the effective values, the looser marker and the way into the editor", async () => {
    mockFetch();
    render(<PODetailScreen id={1} />);
    const heading = await screen.findByRole("heading", { name: "Rules for this PO" });
    const section = heading.closest("section")!;
    expect(within(section).getByRole("link", { name: "Edit rules for this PO" })).toHaveAttribute("href", "/settings/pos/1");
    expect(within(section).getAllByText("looser than default").length).toBeGreaterThan(0);
    expect(section).toHaveTextContent("4 values differ from the global defaults.");
    expect(section).toHaveTextContent("5.00%");
  });
  it("carries no looser marker when the PO inherits everything", async () => {
    mockFetch({ "/api/settings/pos/1": poDefault });
    render(<PODetailScreen id={1} />);
    const section = (await screen.findByRole("heading", { name: "Rules for this PO" })).closest("section")!;
    expect(within(section).queryByText("looser than default")).toBeNull();
    expect(section).toHaveTextContent("Everything inherits the global defaults.");
  });
});

describe("checkSetting (the client-side range check)", () => {
  const f = (key: string) => G.values.find((v) => v.key === key)!;
  it("follows the ranges and steps", () => {
    expect(checkSetting(f("tolerance_pct"), "25")).toBeNull();
    expect(checkSetting(f("tolerance_pct"), "-1")).toBe("At least 0.");
    expect(checkSetting(f("tolerance_abs"), "1.005")).toBe("In steps of 0.01.");
    expect(checkSetting(f("confidence_threshold"), "0.4")).toBe("At least 0.5.");
    expect(checkSetting(f("duplicate_days"), "2.5")).toBe("Whole days only.");
    expect(checkSetting(f("duplicate_days"), "")).toBe("Enter a value.");
    expect(checkSetting(f("tolerance_mode"), "greater_of")).toBeNull();
  });
});

// The settings_applied event ("Settings used"): written once per run in the validate stage before the rules (SPEC section 11 item 93).
const used = (runViewFx as unknown as RunView).settings_used!;
function withSettingsEvent(events: AuditEvent[]): AuditEvent[] {
  const at = events.findIndex((e) => e.stage === "validate");
  const { message, ...detail } = used;
  const ev: AuditEvent = { ...events[at], event_type: "settings_applied", rule_id: null, outcome: "info", message, detail }   // as runner.settings_applied_event writes it;
  return [...events.slice(0, at), ev, ...events.slice(at)].map((e, i) => ({ ...e, seq: i + 1 }));
}
function play(events: AuditEvent[]): RunState {
  return events.reduce((s, e, i) => runReducer(s, { type: "audit", event: e, now: 1000 + i }), initialRunState());
}

describe("the settings_applied event in the live run view", () => {
  const before = play(auditOf(STREAMS.ss10963));
  const after = play(withSettingsEvent(auditOf(STREAMS.ss10963)));
  it("is kept with the validate stage and changes nothing else in the reducer", () => {
    expect(after.stages.validate.events.map((e) => e.event_type)).toContain("settings_applied");
    expect(after.eventCount).toBe(before.eventCount + 1);
    expect(liveRules(after)).toEqual(liveRules(before).map((e) => expect.objectContaining({ rule_id: e.rule_id, outcome: e.outcome })));
    expect(Object.fromEntries(Object.entries(after.stages).map(([k, s]) => [k, s.status])))
      .toEqual(Object.fromEntries(Object.entries(before.stages).map(([k, s]) => [k, s.status])));
    expect(after.decision).toBe(before.decision);
  });
  it("shows the Settings used line on the validate stage card, and in its events when opened", () => {
    render(<StageTimeline state={after} />);
    expect(screen.getByText(used.message)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Validate/ }));
    expect(within(screen.getByRole("list", { name: /Validate events/ })).getByText(used.message)).toBeInTheDocument();
  });
  it("a stream without the event (runs from before settings) still renders, without the line", () => {
    render(<StageTimeline state={before} />);
    expect(screen.queryByText(/^Settings used:/)).toBeNull();
  });
});

describe("Settings used in the run view", () => {
  it("names the PO's settings, marks the overridden values and the switched-off rule, and links to the rules", () => {
    render(<ResultView view={runViewFx as unknown as RunView} />);
    const section = screen.getByRole("heading", { name: "Settings used" }).closest("section")!;
    expect(section).toHaveTextContent(used.message);
    expect(within(section).getAllByText("this PO")).toHaveLength(3);
    expect(section).toHaveTextContent("r_po_line_price");
    expect(within(section).getByRole("link", { name: "Rules for PO-SS-001" })).toHaveAttribute("href", "/settings/pos/1");
  });
  it("a run view from before settings (no settings_used) renders without the section", () => {
    render(<ResultView view={VIEWS.ss10963} />);
    expect(screen.queryByRole("heading", { name: "Settings used" })).toBeNull();
  });
});
