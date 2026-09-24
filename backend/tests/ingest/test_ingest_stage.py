import json

import pytest

from app.config import Settings
from app.enums import Outcome, StageStatus
from app.ingest.stage import run_ingest_stage
from app.ingest.validate import IngestRejected
from app.models import RunContext
from tests.ingest import docs


def s(tmp_path, **kw):
    return Settings(_env_file=None, runs_dir=tmp_path / "runs", **kw)


def ctx(run_id="run-1"):
    return RunContext(run_id=run_id, source_file="whatever.pdf")


def by_type(stage, event_type):
    return [e for e in stage.events if e.event_type == event_type]


def test_native_pdf_is_ingested_end_to_end(tmp_path):
    src = docs.make_native_pdf(tmp_path / "in.pdf", [["Invoice INV-1001 total 2,160.00 USD payable within 30 days"], ["Terms and conditions apply to all orders"]])
    c = ctx()
    stage = run_ingest_stage(c, src, s(tmp_path))
    info = c.ingest
    assert stage.status is StageStatus.OK and stage.stage == "ingest" and info.failure_kind is None
    assert (info.media_type, info.pages_total, info.pages_processed, info.truncated) == ("application/pdf", 2, 2, False)
    assert c.file_hash == info.sha256 and len(info.sha256) == 64 and info.text_layer.usable
    run_dir = tmp_path / "runs" / "run-1"
    assert (run_dir / "original.pdf").read_bytes() == src.read_bytes() and (run_dir / "meta.json").is_file()
    assert [p.number for p in info.pages] == [1, 2] and all((run_dir / "pages" / f"page-{n}.png").is_file() for n in (1, 2))
    assert [(run_dir / "text" / f"page-{n}.txt").read_text(encoding="utf-8").split()[0] for n in (1, 2)] == ["Invoice", "Terms"]
    assert stage.outputs["sha256"] == info.sha256 and stage.outputs["text_layer_usable"] is True


def test_events_tell_the_story(tmp_path):
    stage = run_ingest_stage(ctx(), docs.make_native_pdf(tmp_path / "in.pdf", [["Invoice INV-1001 total 2,160.00 USD payable within 30 days"]]), s(tmp_path))
    assert [e.event_type for e in stage.events] == ["file_validated", "file_stored", "document_rendered"]
    assert all(e.stage == "ingest" for e in stage.events)
    assert stage.events[1].detail["sha256"] == stage.outputs["sha256"] and stage.events[2].outcome is Outcome.PASS


def test_image_input_has_no_text_layer(tmp_path):
    c = ctx()
    stage = run_ingest_stage(c, docs.make_jpeg(tmp_path / "photo.jpg"), s(tmp_path))
    assert stage.status is StageStatus.OK and c.ingest.media_type == "image/jpeg" and c.ingest.text_paths == [None]
    assert not c.ingest.text_layer.usable and (tmp_path / "runs" / "run-1" / "original.jpg").is_file()
    assert not (tmp_path / "runs" / "run-1" / "text" / "page-1.txt").exists()


def test_scanned_pdf_has_no_usable_text_layer(tmp_path):
    c = ctx()
    run_ingest_stage(c, docs.make_scanned_pdf(tmp_path / "scan.pdf", pages=2), s(tmp_path))
    assert c.ingest.pages_processed == 2 and not c.ingest.text_layer.usable and c.ingest.text_layer.reason == "no_text_layer"


def test_page_cap_truncates_and_flags(tmp_path):
    c = ctx()
    stage = run_ingest_stage(c, docs.make_native_pdf(tmp_path / "in.pdf", [[f"PAGE {i} with some filler text to read"] for i in range(1, 5)]),
                             s(tmp_path, max_pages=2))
    assert stage.status is StageStatus.FLAGGED and c.ingest.truncated and (c.ingest.pages_total, c.ingest.pages_processed) == (4, 2)
    assert "pages_truncated:2/4" in c.ingest.issues
    ev = by_type(stage, "pages_truncated")[0]
    assert ev.outcome is Outcome.FLAG and ev.detail == {"pages_total": 4, "max_pages": 2}


def test_password_protected_pdf_is_accepted_but_reported_as_a_vendor_side_failure(tmp_path):
    c = ctx()
    stage = run_ingest_stage(c, docs.make_native_pdf(tmp_path / "locked.pdf", [["x"]], password="pw"), s(tmp_path))
    assert stage.status is StageStatus.FAILED and c.ingest.failure_kind == "vendor_side" and "password" in c.ingest.failure_reason.lower()
    assert "password_protected" in c.ingest.issues and c.ingest.pages == [] and c.file_hash                      # still hashed and stored
    assert (tmp_path / "runs" / "run-1" / "original.pdf").is_file()
    assert by_type(stage, "ingest_failed")[0].outcome is Outcome.FAIL and by_type(stage, "ingest_failed")[0].detail["failure_kind"] == "vendor_side"


def test_corrupt_pdf_is_a_system_side_failure(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4\nnot a pdf")
    c = ctx()
    stage = run_ingest_stage(c, bad, s(tmp_path))
    assert stage.status is StageStatus.FAILED and c.ingest.failure_kind == "system_side" and "corrupt_pdf" in c.ingest.issues


def test_blank_document_is_a_vendor_side_failure(tmp_path):
    c = ctx()
    run_ingest_stage(c, docs.make_blank_pdf(tmp_path / "blank.pdf"), s(tmp_path))
    assert c.ingest.failure_kind == "vendor_side" and "blank_document" in c.ingest.issues


def test_rejected_files_raise_and_leave_no_trace(tmp_path):
    junk = tmp_path / "evil.pdf"
    junk.write_bytes(b"MZ\x90\x00 executable")
    c = ctx()
    with pytest.raises(IngestRejected) as exc:
        run_ingest_stage(c, junk, s(tmp_path))
    assert exc.value.code == "unsupported_type" and not (tmp_path / "runs").exists()
    assert c.ingest is None and c.file_hash is None


def test_oversize_and_missing_files_leave_no_trace_either(tmp_path):
    f = docs.make_png(tmp_path / "a.png")
    with pytest.raises(IngestRejected):
        run_ingest_stage(ctx(), f, s(tmp_path, max_file_bytes=10))
    with pytest.raises(IngestRejected):
        run_ingest_stage(ctx(), tmp_path / "missing.pdf", s(tmp_path))
    assert not (tmp_path / "runs").exists()


def test_invalid_run_id_is_refused_before_anything_is_written(tmp_path):
    with pytest.raises(ValueError):
        run_ingest_stage(RunContext(run_id="../escape", source_file="x"), docs.make_png(tmp_path / "a.png"), s(tmp_path))
    assert not (tmp_path / "runs").exists()


def test_the_same_file_ingested_twice_gets_the_same_hash_in_separate_run_folders(tmp_path):
    src = docs.make_native_pdf(tmp_path / "in.pdf", [["Invoice INV-1001 total 2,160.00 USD payable within 30 days"]])
    a, b = ctx("run-a"), ctx("run-b")
    run_ingest_stage(a, src, s(tmp_path))
    run_ingest_stage(b, src, s(tmp_path))
    assert a.file_hash == b.file_hash and a.ingest.run_dir != b.ingest.run_dir


def test_ingest_is_deterministic_apart_from_the_run_folder(tmp_path):
    src = docs.make_native_pdf(tmp_path / "in.pdf", [["Invoice INV-1001 total 2,160.00 USD payable within 30 days"]])
    a, b = ctx("run-a"), ctx("run-b")
    run_ingest_stage(a, src, s(tmp_path))
    run_ingest_stage(b, src, s(tmp_path))
    strip = lambda i: {k: v for k, v in i.model_dump(mode="json").items() if k not in ("run_dir", "original_path", "pages", "text_paths")}
    assert strip(a.ingest) == strip(b.ingest)


def test_meta_json_holds_system_facts_only(tmp_path):
    c = ctx()
    run_ingest_stage(c, docs.make_native_pdf(tmp_path / "in.pdf", [["Invoice INV-1001 total 2,160.00 USD payable within 30 days"]]), s(tmp_path))
    raw = (tmp_path / "runs" / "run-1" / "meta.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["ingest"]["sha256"] == c.file_hash and data["extraction"] is None
    assert "INV-1001" not in raw and "api_key" not in raw.lower()                    # no invoice text, no secrets


def test_hostile_source_file_names_do_not_matter(tmp_path):
    hostile = tmp_path / "in"
    hostile.mkdir()
    src = docs.make_png(hostile / "..weird 幣 name (1).png")
    c = ctx()
    run_ingest_stage(c, src, s(tmp_path))
    assert c.ingest.original_name == "..weird 幣 name (1).png" and c.ingest.original_path.endswith("original.png")


def test_ingest_stage_result_is_json_serialisable(tmp_path):
    stage = run_ingest_stage(ctx(), docs.make_jpeg(tmp_path / "a.jpg"), s(tmp_path))
    json.loads(stage.model_dump_json())
