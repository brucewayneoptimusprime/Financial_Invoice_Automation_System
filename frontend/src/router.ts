// A small history router: "/" (dashboard), "/invoices" (upload), "/runs/:id", "/pos", "/pos/new", "/pos/:id", "/review",
// "/review/:id". No dependency needed for that.
import { useEffect, useState } from "react";

export type Route =
  | { name: "dashboard" }
  | { name: "upload" }
  | { name: "run"; id: string }
  | { name: "pos" }
  | { name: "poNew" }
  | { name: "erpSync" }
  | { name: "po"; id: number }
  | { name: "review" }
  | { name: "reviewItem"; id: number }
  | { name: "settings" }
  | { name: "settingsPO"; id: number }
  | { name: "missing" };

export function parse(path: string): Route {
  const p = path.replace(/\/+$/, "") || "/";
  if (p === "/") return { name: "dashboard" };
  if (p === "/invoices") return { name: "upload" };
  let m = /^\/runs\/([A-Za-z0-9_-]{1,64})$/.exec(p);
  if (m) return { name: "run", id: m[1] };
  if (p === "/pos") return { name: "pos" };
  if (p === "/pos/new") return { name: "poNew" };
  if (p === "/pos/erp-sync") return { name: "erpSync" };
  m = /^\/pos\/(\d{1,12})$/.exec(p);
  if (m) return { name: "po", id: Number(m[1]) };
  if (p === "/review") return { name: "review" };
  m = /^\/review\/(\d{1,12})$/.exec(p);
  if (m) return { name: "reviewItem", id: Number(m[1]) };
  if (p === "/settings") return { name: "settings" };
  m = /^\/settings\/pos\/(\d{1,12})$/.exec(p);
  if (m) return { name: "settingsPO", id: Number(m[1]) };
  return { name: "missing" };
}

// Screens with unsaved work register a guard; navigation asks before leaving them.
let leaveGuard: (() => boolean) | null = null;
export function setLeaveGuard(guard: (() => boolean) | null): void {
  leaveGuard = guard;
}

export function navigate(path: string): void {
  if (leaveGuard && !leaveGuard()) return;
  leaveGuard = null;
  window.history.pushState(null, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
  window.scrollTo({ top: 0 });
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parse(window.location.pathname));
  useEffect(() => {
    const on = () => setRoute(parse(window.location.pathname));
    window.addEventListener("popstate", on);
    return () => window.removeEventListener("popstate", on);
  }, []);
  return route;
}

export function linkProps(path: string) {
  return {
    href: path,
    onClick: (e: React.MouseEvent) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
      e.preventDefault();
      navigate(path);
    },
  };
}
