"""Where the feed comes from. v1 is a bundled file (`Settings.erp_feed_path`); a real ERP connection (HTTP, SFTP) would be a second
source returning the same `FeedFile`. JSON numbers are parsed as Decimal, so no float ever touches money."""
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.config import Settings


class FeedError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class FeedFile:
    name: str                 # display name (the file name, never a full path)
    sha256: str
    doc: dict[str, Any]


def parse_feed(raw: bytes, name: str, max_bytes: int) -> FeedFile:
    if len(raw) > max_bytes:
        raise FeedError("too_large", f"The feed is {len(raw):,} bytes; at most {max_bytes:,} are read.")
    try:
        doc = json.loads(raw.decode("utf-8-sig"), parse_float=Decimal)
    except (UnicodeDecodeError, ValueError):
        raise FeedError("not_json", "The feed is not valid JSON.") from None
    if not isinstance(doc, dict):
        raise FeedError("not_an_object", "The feed must be a JSON object with a \"purchase_orders\" list.")
    return FeedFile(name=name, sha256=hashlib.sha256(raw).hexdigest(), doc=doc)


class FileFeedSource:
    def __init__(self, settings: Settings):
        self.path = settings.erp_feed_path
        self.max_bytes = settings.erp_feed_max_bytes

    def fetch(self) -> FeedFile:
        try:
            if not self.path.is_file():
                raise FeedError("not_found", f"The simulated ERP feed file {self.path.name} was not found.")
            if self.path.stat().st_size > self.max_bytes:                  # refused before reading it
                raise FeedError("too_large", f"The feed is larger than {self.max_bytes:,} bytes.")
            raw = self.path.read_bytes()
        except OSError:
            raise FeedError("unreadable", f"The simulated ERP feed file {self.path.name} could not be read.") from None
        return parse_feed(raw, self.path.name, self.max_bytes)


def feed_source(settings: Settings) -> FileFeedSource:
    return FileFeedSource(settings)
