"""Generate test documents at test time (reportlab / Pillow / pypdfium2). No binary fixtures are committed and
nothing here comes from a real invoice: the content is generic filler."""
import io
from pathlib import Path

from PIL import Image, ImageDraw


def _draw_content(img: Image.Image, lines: list[str] | None = None) -> Image.Image:
    draw = ImageDraw.Draw(img)
    w, h = img.size
    draw.rectangle([w // 10, h // 10, w - w // 10, h // 10 + 8], fill=0 if img.mode in ("L", "1") else (0, 0, 0)[: len(img.getbands())] or 0)
    for i, line in enumerate(lines or ["INVOICE", "Item one 10.00", "Item two 20.00", "TOTAL 30.00"]):
        draw.text((w // 10, h // 5 + i * 24), line, fill=0 if img.mode in ("L", "1") else (0, 0, 0)[: len(img.getbands())])
    return img


def make_native_pdf(path: Path, pages: list[list[str]], password: str | None = None, owner_only: bool = False) -> Path:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.pdfencrypt import StandardEncryption
    from reportlab.pdfgen import canvas

    encrypt = None
    if password is not None:
        encrypt = StandardEncryption(userPassword="" if owner_only else password, ownerPassword=password, canPrint=0)
    c = canvas.Canvas(str(path), pagesize=letter, encrypt=encrypt)
    for lines in pages:
        for i, line in enumerate(lines):
            c.drawString(72, 720 - 16 * i, line)
        c.showPage()
    c.save()
    return path


def make_blank_pdf(path: Path, pages: int = 1) -> Path:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=letter)
    for _ in range(pages):
        c.showPage()
    c.save()
    return path


def make_scanned_pdf(path: Path, pages: int = 1, size=(1275, 1650)) -> Path:
    """An image-only PDF: pages are pictures, there is NO text layer."""
    images = [_draw_content(Image.new("RGB", size, "white"), [f"SCANNED PAGE {i + 1}", "TOTAL 99.00"]) for i in range(pages)]
    images[0].save(path, "PDF", resolution=150.0, save_all=True, append_images=images[1:])
    return path


def make_zero_page_pdf(path: Path) -> Path:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument.new()
    doc.save(str(path))
    doc.close()
    return path


def make_png(path: Path, size=(800, 600), mode: str = "RGB") -> Path:
    _draw_content(Image.new(mode, size, 255 if mode == "L" else "white")).save(path, "PNG")
    return path


def make_jpeg(path: Path, size=(800, 600)) -> Path:
    _draw_content(Image.new("RGB", size, "white")).save(path, "JPEG", quality=90)
    return path


def make_exif_jpeg(path: Path, size=(200, 100), orientation: int = 6) -> Path:
    exif = Image.Exif()
    exif[0x0112] = orientation
    _draw_content(Image.new("RGB", size, "white")).save(path, "JPEG", exif=exif)
    return path


def make_cmyk_jpeg(path: Path, size=(300, 200)) -> Path:
    img = Image.new("CMYK", size, (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle([20, 20, 120, 80], fill=(0, 255, 255, 0))
    img.save(path, "JPEG")
    return path


def make_transparent_png(path: Path, size=(200, 150)) -> Path:
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle([20, 20, 100, 60], fill=(0, 0, 0, 255))
    img.save(path, "PNG")
    return path


def make_palette_png(path: Path, size=(200, 150)) -> Path:
    img = Image.new("P", size, 0)
    ImageDraw.Draw(img).rectangle([20, 20, 100, 60], fill=1)
    img.putpalette([255, 255, 255, 0, 0, 0] + [0] * 250 * 3)
    img.save(path, "PNG")
    return path


def make_noisy_png(path: Path, size=(2000, 2000)) -> Path:
    import os

    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(path, "PNG")
    return path


def blank_png_bytes(size=(300, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, "PNG")
    return buf.getvalue()
