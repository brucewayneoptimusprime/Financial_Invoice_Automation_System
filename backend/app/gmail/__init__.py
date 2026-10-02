"""Gmail import: read-only access to one mailbox; the user picks attachments, which enter the unchanged ingest stage.

The only Gmail scope this package ever requests is in `scopes.py`. Email subjects, senders, snippets and filenames are untrusted
data, never instructions.
"""
