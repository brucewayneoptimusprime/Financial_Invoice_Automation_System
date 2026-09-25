import { useEffect, useState } from "react";
import { getHealth } from "./api";
import { linkProps, useRoute } from "./router";
import { UploadScreen } from "./screens/Upload";
import { RunScreen } from "./screens/Run";
import { POListScreen } from "./screens/POList";
import { PODetailScreen } from "./screens/PODetail";
import { PONewScreen } from "./screens/PONew";
import { usd } from "./format";
import type { Health } from "./types";

function ModeBadge({ health, error }: { health: Health | null; error: boolean }) {
  if (error) return <span className="mode mode-down" title="The API is not reachable">API offline</span>;
  if (!health) return <span className="mode">…</span>;
  const text = { live: "LIVE · paid API calls", replay: "REPLAY · recorded responses", offline: "OFFLINE · no model" }[health.mode];
  return <span className={`mode mode-${health.mode}`} title={`Model ${health.model}`}>{text}</span>;
}

export function App() {
  const route = useRoute();
  const [health, setHealth] = useState<Health | null>(null);
  const [down, setDown] = useState(false);

  useEffect(() => {
    let stop = false;
    const load = () => getHealth().then((h) => { if (!stop) { setHealth(h); setDown(false); } })
                                   .catch(() => { if (!stop) setDown(true); });
    load();
    const t = window.setInterval(load, 5000);
    return () => { stop = true; window.clearInterval(t); };
  }, []);

  return (
    <div className="app">
      <header className="topbar">
        <a className="brand" {...linkProps("/")}>
          <span className="brand-mark" aria-hidden="true" />
          Invoice Agent
        </a>
        <nav className="nav" aria-label="Main">
          <a {...linkProps("/")} className={route.name === "upload" || route.name === "run" ? "active" : undefined}>Invoices</a>
          <a {...linkProps("/pos")} className={route.name.startsWith("po") ? "active" : undefined}>Purchase orders</a>
        </nav>
        <div className="topbar-right">
          {health && health.session_spent_usd !== null && (
            <span className="spend" title={`Ceilings: ${usd(health.run_ceiling_usd)} per run, ${usd(health.session_ceiling_usd)} per server session`}>
              Spent {usd(health.session_spent_usd)}
            </span>
          )}
          <ModeBadge health={health} error={down} />
        </div>
      </header>
      <main className="page">
        {route.name === "upload" && <UploadScreen key={window.location.search} health={health} />}
        {route.name === "run" && <RunScreen key={route.id} runId={route.id} />}
        {route.name === "pos" && <POListScreen />}
        {route.name === "poNew" && <PONewScreen health={health} />}
        {route.name === "po" && <PODetailScreen key={route.id} id={route.id} />}
        {route.name === "missing" && (
          <div className="empty">
            <h1>Page not found</h1>
            <p><a {...linkProps("/")}>Back to upload</a></p>
          </div>
        )}
      </main>
    </div>
  );
}
