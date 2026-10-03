"""The adapter registry: one adapter per ERP format, chosen by the feed envelope's `format` (a second ERP = one more entry)."""
from app.erp.adapters.base import ERPAdapter, FeedPO, FeedVendor
from app.erp.adapters.simerp_v1 import SimErpV1
from app.erp.source import FeedError

ADAPTERS: tuple[ERPAdapter, ...] = (SimErpV1(),)


def adapter_for(doc: dict, adapters: tuple[ERPAdapter, ...] = ADAPTERS) -> ERPAdapter:
    fmt = doc.get("format")
    for a in adapters:
        if fmt in a.formats:
            return a
    known = ", ".join(f for a in adapters for f in a.formats)
    raise FeedError("unknown_format", f"The feed format {fmt!r} has no adapter (known: {known}).")


__all__ = ["ADAPTERS", "ERPAdapter", "FeedPO", "FeedVendor", "adapter_for"]
