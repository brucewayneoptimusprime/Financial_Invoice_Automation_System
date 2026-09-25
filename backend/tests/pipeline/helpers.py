"""Shared setup for pipeline tests: a demo-seeded database and a scripted model client (no live calls, ever)."""
import json
import shutil
from pathlib import Path

from app.config import Settings
from app.db.connection import connect
from app.db.reset import reset_database
from app.pipeline.runner import run_pipeline
from tests.extraction.helpers import make_source, settings as make_settings
from tests.extraction.real import IQ, REAL, real_pdf, real_reply
from tests.extraction.wire_convert import set_field
from tests.llm.fakes import FakeLLMClient, ok_response

DEMO = Settings(_env_file=None).demo_seed_path
LABEL_CONTROLLED = "CONTROLLED VARIANT (synthetic: a real invoice with an edited-in PO reference)"


def demo_db(tmp_path, name="app.db"):
    """A fresh SQLite database loaded with the demo dataset. The caller closes it."""
    path = tmp_path / name
    reset_database(path, DEMO)
    return connect(path)


def scripted(reply: dict | str, *, input_tokens=6800, output_tokens=920):
    text = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
    return FakeLLMClient(ok_response(text, input_tokens=input_tokens, output_tokens=output_tokens))


def cfg(tmp_path, **kw):
    return make_settings(tmp_path, **kw)


def controlled_variant_reply(name: str, po_reference: str) -> dict:
    """SYNTHETIC. The recorded reply of a real invoice with a purchase-order reference edited in (none of the sample invoices
    prints one). Used only where a test needs the approve path; never one of the real samples."""
    reply = real_reply(name)
    set_field(reply, "po_reference", found=True, value=po_reference, page=1, source_text=f"PO: {po_reference}", confidence=0.95, flag="yes")
    reply["extraction_notes"] = (reply.get("extraction_notes") or "") + " [SYNTHETIC CONTROLLED VARIANT: PO reference edited in for a test]"
    return reply


def run_real(conn, tmp_path, name, reply=None, **kw):
    """Run one of the real fixtures through the whole pipeline with a scripted extraction reply."""
    client = scripted(real_reply(name) if reply is None else reply)
    return run_pipeline(real_pdf(name), conn, client=client, settings=cfg(tmp_path), **kw)


def rows(conn, sql, *args):
    return [dict(r) for r in conn.execute(sql, args)]


def one(conn, sql, *args):
    r = conn.execute(sql, args).fetchone()
    return None if r is None else dict(r)
