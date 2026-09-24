import io
from pathlib import Path

import pytest
from PIL import Image

from app.config import Settings
from app.ingest import render as render_module
from app.ingest.render import render_document
from app.ingest.textlayer import assess_text_layer, clean_text
from tests.ingest import docs

PDF, PNG, JPEG = "application/pdf", "image/png", "image/jpeg"


def s(tmp_path, **kw):
    return Settings(_env_file=None, runs_dir=tmp_path / "runs", **kw)


def render(tmp_path, path, media, **kw):
    return render_document(path, media, tmp_path / "pages", s(tmp_path, **kw))


def open_page(page):
    """A fully loaded copy, so the file handle is closed immediately."""
    with Image.open(page.path) as im:
        return im.copy()


# ------------------------------------------------------------------------------ PDFs

def test_native_pdf_renders_every_page_in_order_with_per_page_text(tmp_path):
    pdf = docs.make_native_pdf(tmp_path / "a.pdf", [["ALPHA-PAGE one"], ["BRAVO-PAGE two"], ["CHARLIE-PAGE three"]])
    r = render(tmp_path, pdf, PDF)
    assert r.failure is None and r.pages_total == 3 and [p.number for p in r.pages] == [1, 2, 3] and not r.truncated
    assert "ALPHA-PAGE" in r.texts[0] and "BRAVO-PAGE" in r.texts[1] and "CHARLIE-PAGE" in r.texts[2]
    assert all(p.path.endswith(f"page-{p.number}.png") and p.media_type == "image/png" for p in r.pages)


def test_page_images_are_capped_at_the_configured_longest_side(tmp_path):
    pdf = docs.make_native_pdf(tmp_path / "a.pdf", [["hello world"]])
    r = render(tmp_path, pdf, PDF)
    img = open_page(r.pages[0])
    assert max(img.size) == 1568 and img.mode == "RGB" and (r.pages[0].width, r.pages[0].height) == img.size
    small = render_document(pdf, PDF, tmp_path / "pages2", s(tmp_path, render_max_side_px=800))
    assert max(open_page(small.pages[0]).size) == 800


def test_pdf_pages_beyond_the_cap_are_not_processed_and_are_flagged(tmp_path):
    pdf = docs.make_native_pdf(tmp_path / "a.pdf", [[f"PAGE-{i}"] for i in range(1, 6)])
    r = render(tmp_path, pdf, PDF, max_pages=2)
    assert r.pages_total == 5 and len(r.pages) == 2 and len(r.texts) == 2 and r.truncated and r.failure is None


def test_scanned_pdf_has_images_and_no_text_layer(tmp_path):
    r = render(tmp_path, docs.make_scanned_pdf(tmp_path / "scan.pdf", pages=2), PDF)
    assert r.failure is None and len(r.pages) == 2 and r.texts == [None, None]


def test_password_protected_pdf_is_a_vendor_side_failure(tmp_path):
    r = render(tmp_path, docs.make_native_pdf(tmp_path / "locked.pdf", [["secret"]], password="pw"), PDF)
    assert r.failure.kind == "vendor_side" and r.failure.code == "password_protected" and r.pages == []
    assert "password" in r.failure.message.lower()


def test_owner_only_encryption_still_renders(tmp_path):
    r = render(tmp_path, docs.make_native_pdf(tmp_path / "own.pdf", [["readable text"]], password="own", owner_only=True), PDF)
    assert r.failure is None and "readable text" in r.texts[0]


def test_corrupt_pdf_is_a_system_side_failure(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4\nthis is not really a pdf\n")
    r = render(tmp_path, bad, PDF)
    assert r.failure.kind == "system_side" and r.failure.code == "corrupt_pdf" and r.pages == []


def test_truncated_real_pdf_is_a_failure_not_a_crash(tmp_path):
    good = docs.make_native_pdf(tmp_path / "a.pdf", [["hello"]]).read_bytes()
    cut = tmp_path / "cut.pdf"
    cut.write_bytes(good[: len(good) // 3])
    r = render(tmp_path, cut, PDF)
    assert r.failure is not None and r.failure.kind == "system_side"


def test_zero_page_pdf_is_a_system_side_failure(tmp_path):
    r = render(tmp_path, docs.make_zero_page_pdf(tmp_path / "zero.pdf"), PDF)
    # pdfium reports an empty document as a format error ("corrupt_pdf"); "no_pages" is the defensive twin.
    assert r.failure.kind == "system_side" and r.failure.code in ("corrupt_pdf", "no_pages") and r.pages == []


def test_all_blank_pdf_is_a_vendor_side_failure_but_one_blank_page_is_only_an_issue(tmp_path):
    r = render(tmp_path, docs.make_blank_pdf(tmp_path / "blank.pdf", pages=2), PDF)
    assert r.failure.kind == "vendor_side" and r.failure.code == "blank_document"
    mixed = docs.make_native_pdf(tmp_path / "mixed.pdf", [["real content here"], []])
    r2 = render(tmp_path, mixed, PDF)
    assert r2.failure is None and r2.issues == ["blank_page:2"] and [p.blank for p in r2.pages] == [False, True]


# ------------------------------------------------------------------------------ images

@pytest.mark.parametrize("maker,media,size", [
    (docs.make_png, PNG, (800, 600)), (docs.make_jpeg, JPEG, (800, 600)), (docs.make_png, PNG, (3000, 2000)),
])
def test_image_files_become_one_page_within_the_size_cap(tmp_path, maker, media, size):
    r = render(tmp_path, maker(tmp_path / "in.bin", size=size), media)
    assert r.failure is None and r.pages_total == 1 and len(r.pages) == 1 and r.texts == [None]
    assert max(open_page(r.pages[0]).size) <= 1568
    assert r.pages[0].media_type == "image/png"


def test_exif_orientation_is_applied(tmp_path):
    r = render(tmp_path, docs.make_exif_jpeg(tmp_path / "rot.jpg", size=(200, 100), orientation=6), JPEG)
    assert (r.pages[0].width, r.pages[0].height) == (100, 200)                       # rotated upright


@pytest.mark.parametrize("orientation,expected", [(1, (200, 100)), (3, (200, 100)), (6, (100, 200)), (8, (100, 200))])
def test_all_common_exif_orientations(tmp_path, orientation, expected):
    r = render(tmp_path, docs.make_exif_jpeg(tmp_path / "o.jpg", orientation=orientation), JPEG)
    assert (r.pages[0].width, r.pages[0].height) == expected


def test_cmyk_palette_and_grayscale_images_become_rgb(tmp_path):
    for name, maker, media in [("cmyk", docs.make_cmyk_jpeg, JPEG), ("pal", docs.make_palette_png, PNG)]:
        r = render(tmp_path / name, maker(tmp_path / f"{name}.bin"), media)
        assert r.failure is None and open_page(r.pages[0]).mode == "RGB", name
    gray = tmp_path / "g.png"
    docs.make_png(gray, mode="L")
    assert open_page(render(tmp_path / "g", gray, PNG).pages[0]).mode == "RGB"


def test_transparency_is_composited_on_white_not_black(tmp_path):
    r = render(tmp_path, docs.make_transparent_png(tmp_path / "t.png"), PNG)
    img = open_page(r.pages[0])
    assert img.getpixel((190, 140)) == (255, 255, 255) and img.getpixel((30, 30)) == (0, 0, 0)
    assert r.failure is None                                                          # not mistaken for a blank page


def test_a_big_noisy_png_falls_back_to_jpeg_under_the_api_size_limit(tmp_path):
    r = render(tmp_path, docs.make_noisy_png(tmp_path / "n.png"), PNG)
    page = r.pages[0]
    assert page.media_type == "image/jpeg" and page.path.endswith(".jpg")
    assert (tmp_path / "pages" / "page-1.jpg").stat().st_size <= 4_500_000


def test_truncated_image_file_is_a_system_side_failure(tmp_path):
    buf = io.BytesIO()
    Image.effect_noise((400, 400), 60).convert("RGB").save(buf, "PNG")
    cut = tmp_path / "cut.png"
    cut.write_bytes(buf.getvalue()[: len(buf.getvalue()) // 2])
    r = render(tmp_path, cut, PNG)
    assert r.failure.kind == "system_side" and r.failure.code == "corrupt_image"


def test_decompression_bombs_are_refused_safely(tmp_path, monkeypatch):
    f = docs.make_png(tmp_path / "big.png", size=(100, 100))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    r = render(tmp_path, f, PNG)
    assert r.failure.kind == "system_side" and r.failure.code == "image_too_large"


def test_blank_image_is_a_vendor_side_failure(tmp_path):
    f = tmp_path / "blank.png"
    f.write_bytes(docs.blank_png_bytes())
    r = render(tmp_path, f, PNG)
    assert r.failure.kind == "vendor_side" and r.failure.code == "blank_document"


def test_rendering_is_deterministic(tmp_path):
    pdf = docs.make_native_pdf(tmp_path / "a.pdf", [["stable output"]])
    a = render_document(pdf, PDF, tmp_path / "p1", s(tmp_path))
    b = render_document(pdf, PDF, tmp_path / "p2", s(tmp_path))
    assert Path(a.pages[0].path).read_bytes() == Path(b.pages[0].path).read_bytes() and a.texts == b.texts


def test_the_renderer_module_never_raises_for_bad_documents(tmp_path):
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"%PDF-" + bytes(range(256)) * 20)
    assert render(tmp_path, junk, PDF).failure is not None
    assert render_module.render_document(junk, PNG, tmp_path / "p", s(tmp_path)).failure.kind == "system_side"


# ------------------------------------------------------------------------------ text cleaning

def test_clean_text_normalises_newlines_controls_and_blank_runs():
    raw = "Line one\r\nLine two\rLine\x00 three  \n\n\n\n\nEnd\x1f  "
    assert clean_text(raw, 1000) == ("Line one\nLine two\nLine three\n\nEnd", False)


def test_clean_text_caps_length_and_reports_it():
    text, cut = clean_text("x" * 500, 100)
    assert len(text) == 100 and cut is True
    assert clean_text("x" * 100, 100) == ("x" * 100, False)


# ------------------------------------------------------------------------------ text layer assessment

GOOD = "Invoice number INV-1001 dated 14 March 2026. Total due 2,160.00 USD. Thank you for your business."


def test_a_normal_text_layer_is_usable():
    info = assess_text_layer([GOOD, GOOD], Settings(_env_file=None))
    assert info.present and info.usable and info.reason == "usable" and info.chars_by_page == [len(GOOD)] * 2
    assert info.wordlike_ratio > 0.9 and info.pages_without_text == []


def test_no_text_layer_means_unusable():
    for texts in ([None], [None, None], [], ["", "  "]):
        info = assess_text_layer(texts, Settings(_env_file=None))
        assert not info.usable and not info.present and info.reason == "no_text_layer"


def test_too_little_text_is_unusable_and_the_threshold_is_configurable():
    assert assess_text_layer(["Page 1"], Settings(_env_file=None)).reason == "too_little_text"
    assert assess_text_layer(["Page 1"], Settings(_env_file=None, text_min_chars_per_page=3)).usable


def test_garbled_text_is_unusable():
    garbage = "��□□ " * 30
    info = assess_text_layer([garbage], Settings(_env_file=None))
    assert info.present and not info.usable and info.reason == "garbled_text"
    private_use = GOOD + "" * (len(GOOD) * 2)               # two thirds private-use junk
    assert assess_text_layer([private_use], Settings(_env_file=None)).reason == "garbled_text"
    assert assess_text_layer([private_use], Settings(_env_file=None, text_min_wordlike_ratio=0.1)).usable   # threshold is config


def test_a_mixed_document_reports_which_pages_lack_text_but_can_still_be_usable():
    info = assess_text_layer([GOOD * 3, None, GOOD * 3], Settings(_env_file=None))
    assert info.usable and info.pages_without_text == [2]


def test_truncated_pages_are_recorded():
    assert assess_text_layer([GOOD], Settings(_env_file=None), truncated_pages=[1]).truncated_pages == [1]


def test_non_latin_text_counts_as_wordlike():
    text = "Счет №1001 от 14 марта 2026 года, итого 2 160,00 руб." * 2
    assert assess_text_layer([text], Settings(_env_file=None)).usable
