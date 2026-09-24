"""Turn a stored file into page images (for the vision model) and per-page embedded text.

Nothing here raises for a bad document: problems come back as a RenderFailure with a `kind`:
  vendor_side  the DOCUMENT is the problem (password-protected, blank)          -> ask the vendor
  system_side  our renderer may be the cause (corrupt/unrenderable, no pages)   -> a human reviews
"""
import io
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageOps, ImageStat, UnidentifiedImageError

from app.config import Settings, get_settings
from app.ingest.textlayer import clean_text
from app.ingest.validate import PDF
from app.models.extraction_meta import FailureKind, PageImageInfo

_MAX_IMAGE_BYTES = 4_500_000          # the API accepts 5 MB per image; stay under it
_BLANK_STDDEV = 1.0


@dataclass(frozen=True)
class RenderFailure:
    kind: FailureKind
    code: str                          # stable: password_protected, blank_document, corrupt_pdf, no_pages, ...
    message: str


@dataclass
class RenderResult:
    pages: list[PageImageInfo] = field(default_factory=list)
    texts: list[str | None] = field(default_factory=list)     # per processed page (None = no text layer)
    truncated_text_pages: list[int] = field(default_factory=list)
    pages_total: int = 0
    truncated: bool = False
    issues: list[str] = field(default_factory=list)
    failure: RenderFailure | None = None


def _to_rgb(img: Image.Image) -> Image.Image:
    """Any mode -> RGB; transparency is composited onto WHITE (not black)."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    return img.convert("RGB")


def _fit(img: Image.Image, max_side: int) -> Image.Image:
    if max(img.size) > max_side:
        img = img.copy()
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return img


def _is_blank(img: Image.Image) -> bool:
    return ImageStat.Stat(img.convert("L")).stddev[0] < _BLANK_STDDEV


def _save_page(img: Image.Image, pages_dir: Path, number: int) -> PageImageInfo:
    """PNG, or JPEG if the PNG would exceed the API's per-image size limit."""
    buffer = io.BytesIO()
    img.save(buffer, "PNG", optimize=False)
    if buffer.tell() <= _MAX_IMAGE_BYTES:
        path, media_type, data = pages_dir / f"page-{number}.png", "image/png", buffer.getvalue()
    else:
        quality = 90
        while True:
            buffer = io.BytesIO()
            img.save(buffer, "JPEG", quality=quality)
            if buffer.tell() <= _MAX_IMAGE_BYTES or quality <= 50:
                break
            quality -= 10
        path, media_type, data = pages_dir / f"page-{number}.jpg", "image/jpeg", buffer.getvalue()
    path.write_bytes(data)
    return PageImageInfo(number=number, path=str(path), width=img.width, height=img.height,
                         media_type=media_type, blank=_is_blank(img))


def _render_pdf(path: Path, pages_dir: Path, settings: Settings) -> RenderResult:
    import pypdfium2 as pdfium

    result = RenderResult()
    try:
        pdf = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        if getattr(exc, "err_code", None) == pdfium.raw.FPDF_ERR_PASSWORD:
            result.failure = RenderFailure("vendor_side", "password_protected",
                                           "The PDF is password-protected and cannot be read without the password.")
        else:
            result.failure = RenderFailure("system_side", "corrupt_pdf",
                                           "The PDF could not be opened (damaged or unsupported format).")
        return result
    except Exception:  # noqa: BLE001 - any other open failure is treated as an unreadable file
        result.failure = RenderFailure("system_side", "corrupt_pdf", "The PDF could not be opened.")
        return result

    try:
        total = len(pdf)
        result.pages_total = total
        if total == 0:
            result.failure = RenderFailure("system_side", "no_pages", "The PDF contains no pages.")
            return result
        processed = min(total, settings.max_pages)
        result.truncated = total > settings.max_pages
        scale = settings.render_dpi / 72
        for index in range(processed):
            number = index + 1
            page = pdf[index]
            try:
                image = _to_rgb(page.render(scale=scale).to_pil())
                result.pages.append(_save_page(_fit(image, settings.render_max_side_px), pages_dir, number))
                try:
                    textpage = page.get_textpage()
                    raw = textpage.get_text_range()
                    textpage.close()
                except Exception:  # noqa: BLE001 - a page without a readable text layer simply has none
                    raw = ""
                text, cut = clean_text(raw, settings.text_max_chars_per_page)
                result.texts.append(text or None)
                if cut:
                    result.truncated_text_pages.append(number)
            except Exception:  # noqa: BLE001
                result.failure = RenderFailure("system_side", "render_failed", f"Page {number} could not be rendered.")
                return result
            finally:
                page.close()
    finally:
        pdf.close()
    return result


def _render_image(path: Path, pages_dir: Path, settings: Settings) -> RenderResult:
    result = RenderResult(pages_total=1)
    try:
        with Image.open(path) as opened:
            opened.load()
            image = _to_rgb(ImageOps.exif_transpose(opened))
    except Image.DecompressionBombError:
        result.failure = RenderFailure("system_side", "image_too_large", "The image has too many pixels to process safely.")
        return result
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        result.failure = RenderFailure("system_side", "corrupt_image", "The image could not be decoded (damaged file).")
        return result
    result.pages.append(_save_page(_fit(image, settings.render_max_side_px), pages_dir, 1))
    result.texts.append(None)
    return result


def render_document(path: Path, media_type: str, pages_dir: Path, settings: Settings | None = None) -> RenderResult:
    """Render to `pages_dir/page-N.png` and extract text. Never raises for a bad document (see RenderFailure)."""
    settings = settings or get_settings()
    pages_dir.mkdir(parents=True, exist_ok=True)
    result = _render_pdf(path, pages_dir, settings) if media_type == PDF else _render_image(path, pages_dir, settings)
    if result.failure is None:
        result.issues = [f"blank_page:{p.number}" for p in result.pages if p.blank]
        if result.pages and all(p.blank for p in result.pages):
            result.failure = RenderFailure("vendor_side", "blank_document", "Every page of the document is blank.")
    return result
