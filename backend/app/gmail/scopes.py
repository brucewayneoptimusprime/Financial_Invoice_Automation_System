"""The ONE OAuth scope the Gmail import requests. A structural test fails if any other Google scope appears in the code."""

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SCOPES: tuple[str, ...] = (GMAIL_READONLY_SCOPE,)
