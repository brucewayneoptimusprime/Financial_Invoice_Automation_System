"""One background thread that runs uploaded invoices one at a time, in upload order (M4 decision 2).

Each run gets its own SQLite connection. Everything a run produces is in the database; the worker keeps in memory only which runs
are still queued or running, and the rare file that ingest rejected after the upload check had passed (no run row exists for it).
"""
import logging
import queue
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.api.uploads import remove_upload
from app.config import Settings
from app.db.connection import connect
from app.ingest.validate import IngestRejected
from app.llm.types import LLMClient
from app.pipeline.runner import run_pipeline

logger = logging.getLogger("app.api.worker")


def open_db(db_path: Path, settings: Settings) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.execute(f"PRAGMA busy_timeout = {int(settings.api_busy_timeout_ms)}")
    return conn


@dataclass(frozen=True)
class Job:
    run_id: str
    path: Path
    source_name: str
    folder: Path            # the upload folder, removed when the run ends
    provenance: dict | None = None   # where the file came from when it was not uploaded (Gmail import); recorded in the audit trail


class RunWorker:
    def __init__(self, db_path: Path, client: LLMClient, settings: Settings, *, run_fn: Callable = run_pipeline):
        self.db_path, self.client, self.settings, self._run_fn = Path(db_path), client, settings, run_fn
        self._queue: "queue.Queue[Job | None]" = queue.Queue()
        self._lock = threading.Lock()
        self._state: dict[str, str] = {}                  # run_id -> queued | running (removed when done)
        self._names: dict[str, str] = {}
        self._rejected: dict[str, dict] = {}              # run_id -> {"code", "message"}
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="run-worker", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout)
            self._thread = None

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """For tests and shutdown: wait until every submitted job has finished."""
        done = threading.Event()

        def check() -> None:
            self._queue.join()
            done.set()
        threading.Thread(target=check, daemon=True).start()
        return done.wait(timeout)

    # ---------------------------------------------------------------------------------------- submit / query
    def submit(self, job: Job) -> None:
        with self._lock:
            self._state[job.run_id] = "queued"
            self._names[job.run_id] = job.source_name
        self._queue.put(job)

    def state(self, run_id: str) -> str | None:
        with self._lock:
            if run_id in self._rejected:
                return "rejected"
            return self._state.get(run_id)

    def rejection(self, run_id: str) -> dict | None:
        with self._lock:
            return self._rejected.get(run_id)

    def pending(self) -> list[dict]:
        with self._lock:
            return [{"id": rid, "source_file": self._names.get(rid), "status": st} for rid, st in self._state.items()]

    @property
    def queue_length(self) -> int:
        with self._lock:
            return len(self._state)

    # ---------------------------------------------------------------------------------------- the loop
    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is None:
                    return
                self._run(job)
            finally:
                self._queue.task_done()

    def _run(self, job: Job) -> None:
        with self._lock:
            self._state[job.run_id] = "running"
        conn = None
        try:
            conn = open_db(self.db_path, self.settings)
            extra = {} if job.provenance is None else {"provenance": job.provenance}
            self._run_fn(job.path, conn, client=self.client, settings=self.settings, run_id=job.run_id, source_name=job.source_name,
                         **extra)
        except IngestRejected as exc:                     # the upload check passed, so this is unexpected; no run row exists
            self._reject(job.run_id, exc.code, exc.message)
        except Exception as exc:                          # noqa: BLE001 - run_pipeline records its own failures; this is before a run exists
            logger.exception("run %s could not start", job.run_id)
            self._reject(job.run_id, "internal_error", f"The run could not start: {type(exc).__name__}.")
        finally:
            if conn is not None:
                conn.close()
            remove_upload(job.folder)
            with self._lock:
                self._state.pop(job.run_id, None)

    def _reject(self, run_id: str, code: str, message: str) -> None:
        with self._lock:
            self._rejected[run_id] = {"code": code, "message": message}
