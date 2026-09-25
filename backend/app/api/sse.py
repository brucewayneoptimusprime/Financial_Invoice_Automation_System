"""The live run stream: Server-Sent Events that tail `audit_events` by `seq` (the database is the only source; SPEC section 1).

Frames:
  retry: 1000                          once, so a dropped browser connection comes back quickly
  event: queued   data {state}          while the run has no `runs` row yet (queued, or ingesting): sent when the state changes
  id: <seq> / event: audit / data row   every stored event with seq > Last-Event-ID, in order, exactly once per connection
  : ping                               a comment line every `sse_heartbeat_s` of silence
  event: end      data {status, decision}   once the run is no longer `running` and every event has gone out; then the stream closes
  event: rejected data {code, message}      the run could not start (no run row will ever exist); then the stream closes

The run status is read BEFORE the events on every poll. The run's final status is committed in the same transaction as its last
event, so a final status seen first guarantees that the events read right after it are complete.
"""
import asyncio
import json
import sqlite3
import time
from contextlib import closing
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol

from starlette.concurrency import run_in_threadpool

from app.api import views

BATCH = 500


class WorkerLike(Protocol):
    def state(self, run_id: str) -> str | None: ...
    def rejection(self, run_id: str) -> dict | None: ...


def frame(event: str | None = None, data: Any = None, id: int | None = None) -> str:
    out = []
    if id is not None:
        out.append(f"id: {id}")
    if event is not None:
        out.append(f"event: {event}")
    if data is not None:
        out.append("data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(out) + "\n\n"


def parse_last_event_id(value: str | None) -> int:
    if value is None or not value.strip().lstrip("-").isdigit():
        return -1
    return max(int(value.strip()), -1)


def _poll(open_conn: Callable[[], sqlite3.Connection], run_id: str, after: int) -> tuple[dict | None, list[dict]]:
    with closing(open_conn()) as conn:
        run = views.run_row(conn, run_id)                                # status FIRST (see the module docstring)
        events = views.events_after(conn, run_id, after, BATCH) if run is not None else []
    return run, events


async def run_events(run_id: str, *, open_conn: Callable[[], sqlite3.Connection], worker: WorkerLike, after: int = -1,
                     poll_s: float = 0.25, heartbeat_s: float = 15.0,
                     is_disconnected: Callable[[], Awaitable[bool]] | None = None) -> AsyncIterator[str]:
    yield "retry: 1000\n\n"
    last, last_sent, waiting_state = after, time.monotonic(), None
    while True:
        if is_disconnected is not None and await is_disconnected():
            return
        run, events = await run_in_threadpool(_poll, open_conn, run_id, last)
        for e in events:
            yield frame("audit", e, id=e["seq"])
            last = e["seq"]
        if events:
            last_sent = time.monotonic()
        if run is None:
            state = worker.state(run_id)
            if state == "rejected":
                yield frame("rejected", worker.rejection(run_id) or {"code": "rejected", "message": "The run could not start."})
                return
            if state is None:
                again, _ = await run_in_threadpool(_poll, open_conn, run_id, last)
                if again is not None:                                    # it finished between the two reads: stream it
                    continue
                yield frame("end", {"status": "unknown", "decision": None})     # neither queued nor stored
                return
            if state != waiting_state:
                waiting_state = state
                yield frame("queued", {"state": state})
                last_sent = time.monotonic()
        elif run["status"] != "running" and len(events) < BATCH:
            yield frame("end", {"status": run["status"], "decision": run["final_decision"]})
            return
        if len(events) >= BATCH:                                         # more are waiting: no sleep
            continue
        if time.monotonic() - last_sent >= heartbeat_s:
            yield ": ping\n\n"
            last_sent = time.monotonic()
        await asyncio.sleep(poll_s)
