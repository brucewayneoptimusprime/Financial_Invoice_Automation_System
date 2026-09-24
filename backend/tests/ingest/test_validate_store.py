import hashlib

import pytest

from app.config import Settings
from app.ingest.store import (
    check_run_id, new_run_id, safe_display_name, sha256_of, store_original, write_run_meta,
)
from app.ingest.validate import IngestRejected, ValidatedFile, sniff_media_type, validate_file
from tests.ingest import docs


def s(**kw):
    return Settings(_env_file=None, **kw)


# ------------------------------------------------------------------------------ type sniffing and validation

def test_accepts_pdf_png_and_jpeg_by_content(tmp_path):
    pdf = docs.make_native_pdf(tmp_path / "a.pdf", [["hello"]])
    png, jpg = docs.make_png(tmp_path / "a.png"), docs.make_jpeg(tmp_path / "a.jpg")
    assert validate_file(pdf, s()).media_type == "application/pdf"
    assert validate_file(png, s()).media_type == "image/png"
    assert validate_file(jpg, s()).media_type == "image/jpeg"
    assert validate_file(png, s()).size_bytes == png.stat().st_size


def test_the_extension_is_never_trusted(tmp_path):
    png_as_pdf = tmp_path / "invoice.pdf"
    png_as_pdf.write_bytes(docs.blank_png_bytes())
    assert validate_file(png_as_pdf, s()).media_type == "image/png"           # a PNG named .pdf is a PNG
    pdf_as_png = tmp_path / "scan.png"
    pdf_as_png.write_bytes(docs.make_native_pdf(tmp_path / "real.pdf", [["x"]]).read_bytes())
    assert validate_file(pdf_as_png, s()).media_type == "application/pdf"


@pytest.mark.parametrize("name,content,fragment", [
    ("evil.pdf", b"MZ\x90\x00\x03\x00\x00\x00 rest of an executable", "Windows executable"),
    ("note.pdf", b"just some plain text, not a pdf at all", "a text file"),
    ("pic.png", b"GIF89a\x01\x00\x01\x00", "GIF"),
    ("doc.pdf", b"PK\x03\x04 zipped office document", "ZIP archive or Office"),
    ("page.pdf", b"<html><body>hello</body></html>", "HTML"),
    ("blob.pdf", bytes(range(128, 200)), "unrecognised binary"),
    ("scan.png", b"II*\x00 tiff data", "TIFF"),
])
def test_unsupported_content_is_rejected_with_a_helpful_message(tmp_path, name, content, fragment):
    f = tmp_path / name
    f.write_bytes(content)
    with pytest.raises(IngestRejected) as exc:
        validate_file(f, s())
    assert exc.value.code == "unsupported_type" and fragment in exc.value.message and "only PDF, PNG and JPEG" in exc.value.message


def test_empty_missing_directory_and_oversize_files_are_rejected(tmp_path):
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    with pytest.raises(IngestRejected) as e1:
        validate_file(empty, s())
    assert e1.value.code == "empty_file"
    with pytest.raises(IngestRejected) as e2:
        validate_file(tmp_path / "nope.pdf", s())
    assert e2.value.code == "not_found"
    with pytest.raises(IngestRejected) as e3:
        validate_file(tmp_path, s())
    assert e3.value.code == "not_a_file"
    big = docs.make_png(tmp_path / "big.png")
    with pytest.raises(IngestRejected) as e4:
        validate_file(big, s(max_file_bytes=100))
    assert e4.value.code == "too_large" and "limit" in e4.value.message


def test_file_exactly_at_the_size_limit_is_accepted(tmp_path):
    f = docs.make_png(tmp_path / "a.png")
    assert validate_file(f, s(max_file_bytes=f.stat().st_size)).size_bytes == f.stat().st_size
    with pytest.raises(IngestRejected):
        validate_file(f, s(max_file_bytes=f.stat().st_size - 1))


def test_allowed_types_come_from_config(tmp_path):
    pdf = docs.make_native_pdf(tmp_path / "a.pdf", [["x"]])
    with pytest.raises(IngestRejected) as exc:
        validate_file(pdf, s(allowed_media_types=("image/png",)))
    assert exc.value.code == "unsupported_type" and "allowed types" in exc.value.message


def test_pdf_header_may_follow_a_little_junk_but_not_a_lot():
    assert sniff_media_type(b"\n\n  junk\n%PDF-1.7\n") == "application/pdf"
    assert sniff_media_type(b"x" * 2000 + b"%PDF-1.7") is None
    assert sniff_media_type(b"") is None


# ------------------------------------------------------------------------------ hashing and storing

def _validated(tmp_path, content: bytes, name="original.pdf", media="application/pdf"):
    f = tmp_path / name
    f.write_bytes(content)
    return ValidatedFile(path=f, media_type=media, size_bytes=len(content))


def test_sha256_matches_a_published_test_vector(tmp_path):
    v = _validated(tmp_path, b"abc")
    stored = store_original(v, "run-a", tmp_path / "runs")
    assert stored.sha256 == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert sha256_of(v.path) == stored.sha256


def test_the_stored_copy_is_byte_identical_and_the_source_is_untouched(tmp_path):
    content = docs.make_native_pdf(tmp_path / "src.pdf", [["hello"]]).read_bytes()
    v = ValidatedFile(path=tmp_path / "src.pdf", media_type="application/pdf", size_bytes=len(content))
    stored = store_original(v, "run-1", tmp_path / "runs")
    assert stored.original_path.read_bytes() == content and (tmp_path / "src.pdf").read_bytes() == content
    assert stored.original_path == tmp_path / "runs" / "run-1" / "original.pdf"
    assert stored.sha256 == hashlib.sha256(content).hexdigest() and stored.size_bytes == len(content)


def test_stored_names_are_generated_per_type(tmp_path):
    for media, ext in [("application/pdf", "pdf"), ("image/png", "png"), ("image/jpeg", "jpg")]:
        stored = store_original(_validated(tmp_path, b"data", f"{ext}.bin", media), f"run-{ext}", tmp_path / "runs")
        assert stored.original_path.name == f"original.{ext}"


def test_same_file_twice_gives_the_same_hash_and_separate_run_folders(tmp_path):
    v = _validated(tmp_path, b"same content")
    a, b = store_original(v, "run-a", tmp_path / "runs"), store_original(v, "run-b", tmp_path / "runs")
    assert a.sha256 == b.sha256 and a.run_dir != b.run_dir and a.original_path.read_bytes() == b.original_path.read_bytes()


def test_an_existing_run_folder_is_never_overwritten(tmp_path):
    v = _validated(tmp_path, b"first")
    first = store_original(v, "run-a", tmp_path / "runs")
    with pytest.raises(IngestRejected) as exc:
        store_original(_validated(tmp_path, b"second", "other.pdf"), "run-a", tmp_path / "runs")
    assert exc.value.code == "run_exists" and first.original_path.read_bytes() == b"first"


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a\\b", "", " ", "x" * 65, "run id", "run..", ".hidden", "run\x00", "C:evil"])
def test_hostile_run_ids_are_refused(bad):
    with pytest.raises(ValueError):
        check_run_id(bad)


def test_generated_run_ids_are_valid_and_unique():
    ids = {new_run_id() for _ in range(50)}
    assert len(ids) == 50 and all(check_run_id(i) == i for i in ids)


@pytest.mark.parametrize("hostile,expected", [
    ("../../etc/passwd", "passwd"), ("C:\\Users\\x\\invoice.pdf", "invoice.pdf"), ("a/b/c.pdf", "c.pdf"),
    ("bad\x00name\x1f.pdf", "badname.pdf"), ("", "unnamed"), ("   ", "unnamed"), ("x" * 500 + ".pdf", "x" * 200),
    ("f\u00fcr \u5e63 invoice.pdf", "f\u00fcr \u5e63 invoice.pdf"),
])
def test_display_names_are_sanitised_but_only_used_as_metadata(hostile, expected):
    assert safe_display_name(hostile) == expected


def test_a_hostile_file_name_cannot_influence_where_the_copy_goes(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    f = src / "weird name \u5e63 (1).pdf"
    f.write_bytes(b"%PDF-1.4 x")
    stored = store_original(ValidatedFile(f, "application/pdf", 10), "run-x", tmp_path / "runs")
    assert stored.original_path.parent == tmp_path / "runs" / "run-x" and stored.original_name == "weird name \u5e63 (1).pdf"
    assert sorted(p.name for p in (tmp_path / "runs" / "run-x").iterdir()) == ["original.pdf"]


def test_run_meta_is_json_with_no_secrets(tmp_path):
    from app.models.extraction_meta import ExtractionMeta, IngestInfo

    info = IngestInfo(media_type="application/pdf", size_bytes=3, sha256="a" * 64, original_name="x.pdf",
                      run_dir=str(tmp_path), original_path=str(tmp_path / "original.pdf"))
    path = write_run_meta(tmp_path, info, ExtractionMeta(cost_usd="0.0123"))
    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ingest"]["sha256"] == "a" * 64 and data["extraction"]["cost_usd"] == "0.0123"
    assert "api_key" not in path.read_text(encoding="utf-8").lower()
