import { useCallback, useEffect, useState } from "react";
import { POForm } from "../components/POForm";
import { linkProps, navigate, setLeaveGuard } from "../router";

type Tab = "form";

// Leaving with unsaved changes asks first (browser close/refresh and in-app navigation).
export function useUnsavedGuard(dirty: boolean) {
  useEffect(() => {
    if (!dirty) { setLeaveGuard(null); return; }
    const onBefore = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", onBefore);
    setLeaveGuard(() => window.confirm("Leave without saving? The purchase order has not been saved."));
    return () => { window.removeEventListener("beforeunload", onBefore); setLeaveGuard(null); };
  }, [dirty]);
}

export function PONewScreen() {
  const [tab] = useState<Tab>("form");
  const [dirty, setDirty] = useState(false);
  useUnsavedGuard(dirty);
  const saved = useCallback((id: number) => { setLeaveGuard(null); navigate(`/pos/${id}`); }, []);
  return (
    <div className="po-new">
      <div className="run-title">
        <a {...linkProps("/pos")} className="back">← Purchase orders</a>
        <h1>New purchase order</h1>
      </div>
      <div className="tabs" role="tablist">
        <button type="button" role="tab" aria-selected={tab === "form"} className="tab active">Form</button>
      </div>
      <div className="section">
        <POForm onSaved={saved} onDirtyChange={setDirty} />
      </div>
    </div>
  );
}
