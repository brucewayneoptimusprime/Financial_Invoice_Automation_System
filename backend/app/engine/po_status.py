"""PO status derived from the ledger, plus the ledger-entry convention for approvals.

Status is a function of (PO total, net committed). `closed` is only ever set by a human and is never
changed automatically. An approval commits the invoice's FULL amount (never capped), so a PO approved
slightly over balance ends up with a negative derived balance and status `fully_billed`.
"""
from app.enums import LedgerType, POStatus


def derive_po_status(total_minor: int, net_committed_minor: int, current: POStatus = POStatus.OPEN) -> POStatus:
    if current == POStatus.CLOSED:
        return POStatus.CLOSED
    if net_committed_minor <= 0:
        return POStatus.OPEN
    if net_committed_minor >= total_minor:
        return POStatus.FULLY_BILLED
    return POStatus.PARTIALLY_BILLED


def commit_entry(invoice_total_minor: int) -> tuple[int, LedgerType]:
    """(amount, type) of the ledger row written when an invoice is approved: the full amount, positive."""
    if invoice_total_minor <= 0:
        raise ValueError("cannot commit a non-positive amount")
    return invoice_total_minor, LedgerType.COMMIT


def reversal_entry(committed_minor: int) -> tuple[int, LedgerType]:
    """(amount, type) that exactly undoes a previous commit: negative, same magnitude."""
    if committed_minor <= 0:
        raise ValueError("can only reverse a positive commit")
    return -committed_minor, LedgerType.REVERSAL
