"""Closed vocabularies shared by the DB schema, config and Pydantic models.

The DB CHECK constraints in db/schema.sql must list the same values.
"""
from enum import StrEnum


class Decision(StrEnum):
    APPROVE = "approve"
    REVIEW = "review"
    REQUEST_INFO = "request_info"
    REJECT = "reject"


class VendorStatus(StrEnum):
    APPROVED = "approved"
    NEW = "new"
    BLOCKED = "blocked"


class POStatus(StrEnum):
    OPEN = "open"
    PARTIALLY_BILLED = "partially_billed"
    FULLY_BILLED = "fully_billed"
    CLOSED = "closed"


class InvoiceStatus(StrEnum):
    """Effective outcome of an invoice. May be changed by a human resolution."""
    PENDING = "pending"
    APPROVED = "approved"
    IN_REVIEW = "in_review"
    AWAITING_INFO = "awaiting_info"
    REJECTED = "rejected"


class LedgerType(StrEnum):
    COMMIT = "commit"
    REVERSAL = "reversal"


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Outcome(StrEnum):
    PASS = "pass"
    FLAG = "flag"
    FAIL = "fail"
    INFO = "info"


class RuleSource(StrEnum):
    BUILTIN = "builtin"
    USER = "user"
    NL = "nl"


class StageStatus(StrEnum):
    OK = "ok"
    FLAGGED = "flagged"
    FAILED = "failed"


class QueueStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class Resolution(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class DraftKind(StrEnum):
    VENDOR_EMAIL = "vendor_email"
    NOTIFICATION = "notification"


class DraftStatus(StrEnum):
    DRAFT = "draft"
    MARKED_SENT = "marked_sent"


class GroundingStatus(StrEnum):
    """How well an extracted field is supported by the document's own text (set by the grounding check)."""
    EXACT = "exact"                    # source_text found verbatim on the page
    NORMALIZED = "normalized"          # found after case/whitespace/ligature/minus normalisation
    FUZZY = "fuzzy"                    # a close (not identical) match
    VALUE_PRESENT = "value_present"    # snippet not found, but the VALUE itself is on the page
    NOT_FOUND = "not_found"            # neither snippet nor value found on the page
    VALUE_MISMATCH = "value_mismatch"  # the value disagrees with its own source_text
    NO_SOURCE = "no_source"            # a value with no source_text
    UNAVAILABLE = "unavailable"        # no usable text layer to check against


class MatchStatus(StrEnum):
    """Outcome of the PO matching step. Only MATCHED means a confident, unambiguous match."""
    MATCHED = "matched"
    NO_CANDIDATES = "no_candidates"
    LOW_SCORE = "low_score"
    AMBIGUOUS = "ambiguous"
