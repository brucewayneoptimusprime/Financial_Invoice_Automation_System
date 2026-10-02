"""One error type for everything the Gmail import can refuse or fail at. The message is plain, safe to show, and never contains a
token, a key, a Google error body or email content."""

STATUS_FOR_CODE = {
    "not_set_up": 503,           # GMAIL_BACKEND disabled or a secret missing
    "not_connected": 409,        # no account connected yet
    "reconnect": 409,            # the stored credential is unusable (expired, revoked, other key)
    "query_invalid": 422,        # the search query failed the operator allowlist
    "translation_failed": 422,   # the sentence could not be turned into an acceptable query (the manual box stays)
    "confirm_required": 400,     # an import must be confirmed explicitly
    "nothing_selected": 422,     # an import with no attachments
    "search_expired": 409,       # import from a search that is too old or unknown
    "not_in_results": 422,       # import of something the search did not show
    "too_many": 422,             # more attachments than one import may take
    "budget": 409,               # the session's model budget cannot cover the import
    "not_found": 404,
    "too_large": 413,
    "rate_limited": 429,
    "unavailable": 502,          # Gmail could not be reached or answered with an error
}


class GmailError(Exception):
    def __init__(self, code: str, message: str, *, problems: list[str] | None = None, detail: dict | None = None):
        if code not in STATUS_FOR_CODE:
            raise ValueError(f"unknown Gmail error code {code!r}")
        super().__init__(message)
        self.code, self.message, self.problems, self.detail = code, message, problems or [], detail or {}

    @property
    def status(self) -> int:
        return STATUS_FOR_CODE[self.code]

    def body(self) -> dict:
        out = {"error": self.code, "message": self.message}
        if self.problems:
            out["problems"] = self.problems
        out.update(self.detail)
        return out
