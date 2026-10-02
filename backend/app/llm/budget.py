"""Cost ceilings. One tracker per process is the "session"; each run has its own sub-total.

A call is first RESERVED (its worst-case cost counts against both ceilings while in flight), then either
SETTLED with the real cost or RELEASED if it failed. Reserving makes the check safe under concurrency.
"""
import threading
from dataclasses import dataclass
from decimal import Decimal

from app.llm.errors import CostCeilingExceeded

_UNATTRIBUTED = "<unattributed>"


@dataclass(frozen=True)
class Reservation:
    run_key: str
    amount: Decimal


def _money(value: Decimal) -> str:
    return f"${value:.4f}"


class CostTracker:
    def __init__(self, per_run_ceiling: Decimal, per_session_ceiling: Decimal):
        self.per_run_ceiling = Decimal(per_run_ceiling)
        self.per_session_ceiling = Decimal(per_session_ceiling)
        self._lock = threading.Lock()
        self._run_spent: dict[str, Decimal] = {}
        self._run_reserved: dict[str, Decimal] = {}
        self._session_spent = Decimal(0)
        self._session_reserved = Decimal(0)

    # ---- inspection (safe to call any time)
    def run_spent(self, run_id: str | None) -> Decimal:
        with self._lock:
            return self._run_spent.get(run_id or _UNATTRIBUTED, Decimal(0))

    @property
    def session_spent(self) -> Decimal:
        with self._lock:
            return self._session_spent

    def remaining(self) -> Decimal:
        """What the session ceiling still allows: ceiling minus spent minus in-flight reservations (never negative)."""
        with self._lock:
            return max(Decimal(0), self.per_session_ceiling - self._session_spent - self._session_reserved)

    # ---- protocol
    def reserve(self, run_id: str | None, projected: Decimal) -> Reservation:
        key = run_id or _UNATTRIBUTED
        with self._lock:
            run_total = self._run_spent.get(key, Decimal(0)) + self._run_reserved.get(key, Decimal(0)) + projected
            if run_total > self.per_run_ceiling:
                raise CostCeilingExceeded(
                    f"Per-run cost ceiling {_money(self.per_run_ceiling)} would be exceeded "
                    f"(spent {_money(self._run_spent.get(key, Decimal(0)))}, this call could cost up to {_money(projected)}). "
                    "The call was not made.")
            session_total = self._session_spent + self._session_reserved + projected
            if session_total > self.per_session_ceiling:
                raise CostCeilingExceeded(
                    f"Per-session cost ceiling {_money(self.per_session_ceiling)} would be exceeded "
                    f"(spent {_money(self._session_spent)}, this call could cost up to {_money(projected)}). "
                    "The call was not made.")
            self._run_reserved[key] = self._run_reserved.get(key, Decimal(0)) + projected
            self._session_reserved += projected
        return Reservation(key, projected)

    def settle(self, reservation: Reservation, actual: Decimal) -> None:
        with self._lock:
            self._release_locked(reservation)
            self._run_spent[reservation.run_key] = self._run_spent.get(reservation.run_key, Decimal(0)) + actual
            self._session_spent += actual

    def release(self, reservation: Reservation) -> None:
        """The call failed before producing billable output: give the reservation back."""
        with self._lock:
            self._release_locked(reservation)

    def _release_locked(self, reservation: Reservation) -> None:
        self._run_reserved[reservation.run_key] = self._run_reserved.get(reservation.run_key, Decimal(0)) - reservation.amount
        self._session_reserved -= reservation.amount


_session_tracker: CostTracker | None = None
_session_lock = threading.Lock()


def get_session_tracker(per_run: Decimal | None = None, per_session: Decimal | None = None) -> CostTracker:
    """The process-wide tracker (the "session"), created on first use from settings."""
    global _session_tracker
    with _session_lock:
        if _session_tracker is None:
            from app.config import get_settings

            s = get_settings()
            _session_tracker = CostTracker(per_run if per_run is not None else s.cost_ceiling_per_run_usd,
                                           per_session if per_session is not None else s.cost_ceiling_per_session_usd)
        return _session_tracker


def reset_session_tracker() -> None:
    """For tests and long-lived processes that want a fresh session."""
    global _session_tracker
    with _session_lock:
        _session_tracker = None
