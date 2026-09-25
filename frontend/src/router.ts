// A two-route history router: "/" (upload) and "/runs/:id" (live run / result). No dependency needed for that.
import { useEffect, useState } from "react";

export type Route = { name: "upload" } | { name: "run"; id: string } | { name: "missing" };

export function parse(path: string): Route {
  if (path === "/" || path === "") return { name: "upload" };
  const m = /^\/runs\/([A-Za-z0-9_-]{1,64})\/?$/.exec(path);
  return m ? { name: "run", id: m[1] } : { name: "missing" };
}

export function navigate(path: string): void {
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
