"""Gmail import service: status, search (validated query -> preview list), the search sessions that bound what may be imported, and
the import itself.

A search writes nothing to the database. It opens a short-lived session holding the candidate set (every listed, eligible
attachment); an import may only take attachments from that set, at most `gmail_max_import_per_action` at a time, and only if the
session's model budget covers them at the per-run ceiling. Each imported file goes through the SAME acceptance check as an upload
(`validate_file`) and the SAME worker queue into the unchanged pipeline, with its provenance recorded. Email text is cleaned and
treated as data; a deterministic scan flags sender / subject / snippet / filenames that address an AI (no model sees any of it).
"""
import hashlib
import logging
import sqlite3

import httpx
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from app.api.uploads import disk_name, display_name, remove_upload
from app.api.worker import Job
from app.config import Settings
from app.db.connection import connect
from app.ingest.validate import IngestRejected, validate_file
from app.extraction.injection import scan_text
from app.gmail import store
from app.gmail.attachments import _header, eligibility, walk_parts
from app.gmail.client import GmailClient, RawMessage
from app.gmail.errors import GmailError
from app.gmail.fake import FakeGmailClient
from app.gmail.models import AttachmentInfo, MessageSummary, SearchResult, clean_text
from app.gmail.crypto import GmailCryptoError, TokenCipher
from app.gmail.google_client import GoogleGmailClient
from app.gmail.oauth import OAuthFailed, OAuthFlows
from app.gmail.query import check_and_finalize
from app.llm.budget import CostTracker

logger = logging.getLogger("app.gmail")

MAX_SESSIONS = 20
SENDER_CAP, SUBJECT_CAP, SNIPPET_CAP = 200, 300, 300


@dataclass(frozen=True)
class Candidate:
    """One importable attachment as the search showed it."""
    message_id: str
    part_id: str
    filename: str
    mime_type: str
    size: int
    sender: str | None
    message_date: str | None


@dataclass
class SearchSession:
    search_id: str
    account: str
    created: float
    candidates: dict[tuple[str, str], Candidate] = field(default_factory=dict)


def _iso_from_ms(value) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class GmailService:
    def __init__(self, settings: Settings, db_path: Path, *, client: GmailClient | None = None, tracker: CostTracker | None = None,
                 http: httpx.Client | None = None, clock: Callable[[], float] = time.monotonic, today: Callable[[], date] | None = None):
        self.settings, self.db_path, self.tracker = settings, Path(db_path), tracker
        self._client_override = client
        self._http, self._owns_http = http, http is None
        self._oauth: OAuthFlows | None = None
        self._google: GoogleGmailClient | None = None
        self._google_key: tuple | None = None
        self._google_account: str | None = None
        self._fake: FakeGmailClient | None = None
        self._clock = clock
        self._today = today or (lambda: datetime.now(timezone.utc).date())
        self._sessions: dict[str, SearchSession] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ backend and client
    @property
    def backend(self) -> str:
        return self.settings.gmail_backend_effective()

    def _db(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def client(self) -> GmailClient:
        backend = self.backend
        if backend == "disabled":
            missing = self.settings.gmail_missing()
            why = f"missing {', '.join(missing)}" if missing and self.settings.gmail_backend != "disabled" else "GMAIL_BACKEND=disabled"
            raise GmailError("not_set_up", f"Gmail import is not set up ({why}).")
        if self._client_override is not None:
            return self._client_override
        if backend == "fake":
            if self._fake is None:
                self._fake = FakeGmailClient(self.settings.gmail_fake_inbox)
            return self._fake
        return self._google_client()

    # ------------------------------------------------------------------ the real backend: HTTP, OAuth, the stored credential
    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self.settings.gmail_http_timeout_s)
        return self._http

    @property
    def oauth(self) -> OAuthFlows:
        if self._oauth is None:
            self._oauth = OAuthFlows(self.settings, self.http, clock=self._clock)
        return self._oauth

    def close(self) -> None:
        if self._owns_http and self._http is not None:
            self._http.close()
            self._http = None

    def _forget(self) -> None:
        self._google = self._google_key = self._google_account = None

    def _drop_credential(self) -> None:
        """Google no longer accepts the stored refresh token (expired or revoked): delete it, so the UI asks to connect again."""
        with closing(self._db()) as conn:
            store.delete_credentials(conn)
        self._forget()

    def _refresh_token(self, cred: dict) -> str:
        try:
            return TokenCipher.from_settings(self.settings).decrypt(cred["refresh_token_enc"], cred["key_fingerprint"])
        except GmailCryptoError as exc:
            raise GmailError("reconnect", exc.message) from None

    def _google_client(self) -> GoogleGmailClient:
        with closing(self._db()) as conn:
            cred = store.load_credential(conn)
        if cred is None:
            self._forget()
            raise GmailError("not_connected", "No Gmail account is connected.")
        key = (cred["account_email"], cred["updated_at"], cred["key_fingerprint"])
        if self._google is None or self._google_key != key:
            self._google = GoogleGmailClient(self.http, self.oauth, self._refresh_token(cred), on_reconnect=self._drop_credential,
                                             clock=self._clock)
            self._google_key, self._google_account = key, cred["account_email"]
        return self._google

    def account(self, client: GmailClient) -> str:
        if client is self._google and self._google_account:
            return self._google_account                                   # the stored account: no profile call per search
        return client.profile()

    def start_connect(self) -> tuple[str, str]:
        """(Google's authorization URL, the binding cookie value). Only for the real backend."""
        if self.backend != "google":
            raise GmailError("not_set_up", "Connecting an account needs the real Gmail backend (GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, "
                                           "OAUTH_ENCRYPTION_KEY).")
        return self.oauth.start()

    def finish_connect(self, *, code: str | None, state: str | None, error: str | None, binding: str | None) -> str:
        """The OAuth callback: validate, exchange, read the account, store the refresh token encrypted. Raises OAuthFailed."""
        if self.backend != "google":
            raise GmailError("not_set_up", "Gmail import is not set up.")
        tokens = self.oauth.finish(code=code, state=state, error=error, binding=binding)
        cipher = TokenCipher.from_settings(self.settings)
        client = GoogleGmailClient(self.http, self.oauth, tokens.refresh_token, access_token=tokens.access_token,
                                   expires_in=tokens.expires_in, clock=self._clock)
        try:
            account = client.profile()
        except GmailError:
            account = ""
        if not account:
            self.oauth.revoke(tokens.refresh_token)
            raise OAuthFailed("exchange_failed")
        with closing(self._db()) as conn:
            store.save_credential(conn, account_email=account, scopes=" ".join(tokens.scopes),
                                  refresh_token_enc=cipher.encrypt(tokens.refresh_token), key_fingerprint=cipher.fingerprint)
        self._forget()
        return account

    def disconnect(self) -> dict:
        """Revoke at Google (best effort), then delete the stored credential whatever Google answered."""
        with closing(self._db()) as conn:
            cred = store.load_credential(conn)
        revoked = False
        if cred is not None:
            try:
                revoked = self.oauth.revoke(self._refresh_token(cred))
            except GmailError:
                revoked = False                                           # the token cannot be read with this key: nothing to revoke
            with closing(self._db()) as conn:
                store.delete_credentials(conn)
        self._forget()
        return {"disconnected": cred is not None, "revoked": revoked}

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        s, backend = self.settings, self.backend
        out = {"backend": backend, "available": backend != "disabled", "fake": backend == "fake",
               "missing": s.gmail_missing() if backend == "disabled" and s.gmail_backend != "disabled" else [],
               "connected": False, "account_email": None, "connected_at": None, "reconnect": False, "translator_available": False,
               "caps": {"max_results": s.gmail_max_results, "max_import": s.gmail_max_import_per_action,
                        "query_max_chars": s.gmail_query_max_chars, "request_max_chars": s.gmail_request_max_chars,
                        "default_window_days": s.gmail_default_window_days},
               "budget_remaining_usd": None if self.tracker is None else str(self.tracker.remaining().quantize(Decimal("0.01"))),
               "run_ceiling_usd": str(s.cost_ceiling_per_run_usd)}
        if backend == "disabled":
            return out
        if backend == "fake" or self._client_override is not None:
            try:
                out.update(connected=True, account_email=self.account(self.client()))
            except GmailError:
                pass
            return out
        with closing(self._db()) as conn:
            cred = store.load_credential(conn)
        if cred is not None:
            try:
                self._refresh_token(cred)                                 # readable with the current key?
                out.update(connected=True, account_email=cred["account_email"], connected_at=cred["created_at"])
            except GmailError:
                out.update(reconnect=True)
        return out

    # ------------------------------------------------------------------ search
    def search(self, query_text: str) -> SearchResult:
        client = self.client()
        _, final, added = check_and_finalize(query_text, self.settings, self._today())
        try:
            account = self.account(client)
            ids, estimate = client.search(final, self.settings.gmail_max_results)
            raws = [client.message(i) for i in ids]
        except GmailError:
            raise
        except Exception as exc:                                       # noqa: BLE001 - never surface a stack trace or a Google body
            logger.warning("gmail search failed: %s", type(exc).__name__)
            raise GmailError("unavailable", "Gmail could not be searched just now. Try again.") from None
        with closing(self._db()) as conn:
            imported = store.imported_parts(conn, account, ids)
        session = SearchSession(uuid.uuid4().hex, account, self._clock())
        messages = [self._summarize(raw, imported, session) for raw in raws]
        self._remember(session)
        return SearchResult(search_id=session.search_id, backend=self.backend, account_email=account, query_sent=final,
                            added_terms=added, result_estimate=max(estimate, len(ids)), truncated=estimate > len(ids),
                            messages=messages)

    def _summarize(self, raw: RawMessage, imported: dict, session: SearchSession) -> MessageSummary:
        s = self.settings
        payload = raw.get("payload") or {}
        message_id = str(raw.get("id"))
        sender = clean_text(_header(payload, "From"), SENDER_CAP)
        subject = clean_text(_header(payload, "Subject"), SUBJECT_CAP)
        snippet = clean_text(raw.get("snippet"), SNIPPET_CAP)
        sent = _iso_from_ms(raw.get("internalDate"))
        parts = walk_parts(payload)
        listed = parts[:s.gmail_max_attachments_per_message]
        attachments = []
        for p in listed:
            ok, code, reason = eligibility(p, s)
            run_id = imported.get((message_id, p.part_id, p.size))
            attachments.append(AttachmentInfo(part_id=p.part_id, filename=p.filename, mime_type=p.mime_type, size_bytes=p.size,
                                              inline=p.inline, eligible=ok, reason_code=code, reason=reason, imported_run_id=run_id))
            if ok:
                session.candidates[(message_id, p.part_id)] = Candidate(message_id, p.part_id, p.filename, p.mime_type, p.size,
                                                                        sender, sent)
        fields = {"sender": sender, "subject": subject, "snippet": snippet}
        flagged = [name for name, text in fields.items() if text and scan_text(text, s.injection_patterns)]
        if any(scan_text(p.filename, s.injection_patterns) for p in listed):
            flagged.append("filename")
        return MessageSummary(message_id=message_id, sender=sender, subject=subject, date=sent, snippet=snippet,
                              attachments=attachments, more_attachments=len(parts) - len(listed),
                              reader_instructions=bool(flagged), reader_instruction_fields=flagged)

    # ------------------------------------------------------------------ sessions
    def _remember(self, session: SearchSession) -> None:
        with self._lock:
            self._prune_locked()
            self._sessions[session.search_id] = session
            while len(self._sessions) > MAX_SESSIONS:
                self._sessions.pop(next(iter(self._sessions)))

    def _prune_locked(self) -> None:
        horizon = self._clock() - self.settings.gmail_search_ttl_s
        for sid in [sid for sid, sess in self._sessions.items() if sess.created < horizon]:
            del self._sessions[sid]

    def session(self, search_id: str) -> SearchSession:
        with self._lock:
            self._prune_locked()
            found = self._sessions.get(search_id)
        if found is None:
            raise GmailError("search_expired", "That search has expired. Search again, then pick the attachments.")
        return found

    # ------------------------------------------------------------------ import (the only path from Gmail into the pipeline)
    def check_budget(self, count: int) -> None:
        """Refuse an import the session cannot cover, counting each run at the per-run ceiling (decision 12). Nothing is imported."""
        if self.tracker is None:
            return
        per_run, remaining = Decimal(self.settings.cost_ceiling_per_run_usd), self.tracker.remaining()
        fits = count if per_run <= 0 else int(remaining // per_run)
        if count > fits:
            plural = "" if fits == 1 else "s"
            raise GmailError("budget", f"The model budget left for this server session (${remaining:.2f}) covers at most {fits} "
                                       f"import{plural} at the ${per_run} per-run ceiling. Pick {fits} or fewer.",
                             detail={"fits": fits, "remaining_usd": str(remaining.quantize(Decimal("0.01")))})

    def import_items(self, search_id: str, items: list[tuple[str, str]], *, confirm: bool, submit: Callable[[Job], None]) -> list[dict]:
        """Import the picked attachments of one search. Returns one outcome per item, in order:
        queued (with its run) | already_imported | already_processed (with the existing run) | refused (with the reason)."""
        if confirm is not True:
            raise GmailError("confirm_required", "Confirm the import: nothing is imported without an explicit confirmation.")
        if not items:
            raise GmailError("nothing_selected", "Pick at least one attachment to import.")
        if len(items) > self.settings.gmail_max_import_per_action:
            raise GmailError("too_many", f"At most {self.settings.gmail_max_import_per_action} attachments can be imported at a time.")
        if len(set(items)) != len(items):
            raise GmailError("not_in_results", "The same attachment was picked twice.")
        session = self.session(search_id)
        unknown = [f"{m} / part {p}" for m, p in items if (m, p) not in session.candidates]
        if unknown:
            raise GmailError("not_in_results", "Only attachments listed as importable by that search can be imported.", problems=unknown)
        self.check_budget(len(items))
        client = self.client()
        try:
            account = self.account(client)
        except GmailError:
            raise
        except Exception as exc:                                       # noqa: BLE001
            logger.warning("gmail profile failed: %s", type(exc).__name__)
            raise GmailError("unavailable", "Gmail could not be reached just now. Try again.") from None
        if account != session.account:
            raise GmailError("search_expired", "The connected Gmail account changed since that search. Search again.")
        return [self._import_one(client, session, session.candidates[key], submit) for key in items]

    def _import_one(self, client: GmailClient, session: SearchSession, cand: Candidate, submit: Callable[[Job], None]) -> dict:
        base = {"message_id": cand.message_id, "part_id": cand.part_id, "filename": cand.filename}
        try:
            data = client.attachment(cand.message_id, cand.part_id, self.settings.max_file_bytes)
        except GmailError as exc:
            return {**base, "status": "refused", "reason": exc.message}
        except Exception as exc:                                       # noqa: BLE001 - one failure does not stop the others
            logger.warning("gmail attachment download failed: %s", type(exc).__name__)
            return {**base, "status": "refused", "reason": "The attachment could not be downloaded from Gmail."}
        sha = hashlib.sha256(data).hexdigest()
        with closing(self._db()) as conn:
            earlier = store.import_run(conn, session.account, cand.message_id, sha)
            if earlier is not None:
                return {**base, "status": "already_imported", "run_id": earlier,
                        "reason": "This attachment from this email was imported before."}
            processed = store.processed_run(conn, session.account, sha)
            if processed is not None:
                return {**base, "status": "already_processed", "run_id": processed,
                        "reason": "The same file was already processed (uploaded, or imported from another email)."}
        run_id = uuid.uuid4().hex
        folder = self.settings.api_upload_dir / run_id
        try:
            folder.mkdir(parents=True, exist_ok=False)
            dest = folder / disk_name(cand.filename)
            dest.write_bytes(data)
            validated = validate_file(dest, self.settings)              # the same acceptance check as an upload (magic bytes)
        except IngestRejected as exc:
            remove_upload(folder)
            return {**base, "status": "refused", "reason": exc.message}
        except OSError as exc:
            remove_upload(folder)
            logger.warning("gmail import could not store the file: %s", type(exc).__name__)
            return {**base, "status": "refused", "reason": "The file could not be stored for processing."}
        provenance = {"source": "gmail", "account_email": session.account, "message_id": cand.message_id, "part_id": cand.part_id,
                      "sender": cand.sender, "message_date": cand.message_date, "filename": cand.filename, "attachment_sha256": sha}
        try:
            with closing(self._db()) as conn:
                store.record_import(conn, account_email=session.account, message_id=cand.message_id, attachment_sha256=sha,
                                    part_id=cand.part_id, filename=cand.filename, mime_type=validated.media_type,
                                    size_bytes=validated.size_bytes, sender=cand.sender, message_date=cand.message_date, run_id=run_id)
        except sqlite3.IntegrityError:                                  # a concurrent import of the same file won the race
            remove_upload(folder)
            with closing(self._db()) as conn:
                earlier = store.import_run(conn, session.account, cand.message_id, sha)
            return {**base, "status": "already_imported", "run_id": earlier,
                    "reason": "This attachment from this email was imported before."}
        submit(Job(run_id=run_id, path=dest, source_name=display_name(cand.filename), folder=folder, provenance=provenance))
        return {**base, "status": "queued", "run_id": run_id, "media_type": validated.media_type, "size_bytes": validated.size_bytes}
