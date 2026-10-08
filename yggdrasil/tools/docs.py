"""Document parsing for files and downloaded links (sync; call via ``asyncio.to_thread``).

PDFs use pypdfium2 (text layer, or rendered page images for scans); office / epub /
html / csv use markitdown; plain text and code are decoded directly.
"""

import io
import re
import zipfile
from dataclasses import dataclass, field
from functools import cache
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from yggdrasil.tools.errors import ToolError

if TYPE_CHECKING:
    import pypdfium2
    from markitdown import MarkItDown

# Average non-whitespace characters per page below which a PDF is treated as a scan.
_SCANNED_CHARS_PER_PAGE = 16
_SCAN_RENDER_PAGES = 4
_SCAN_RENDER_SCALE = 1.5
_ZIP_LIST_LIMIT = 200

_TEXT_EXTS = frozenset(
    (
        ".txt .text .md .markdown .rst .log .json .jsonl .yaml .yml .toml .ini .cfg .conf .xml .svg .tex .srt .ass "
        ".vtt .py .pyi .js .mjs .cjs .ts .tsx .jsx .vue .java .kt .kts .c .h .cpp .hpp .cc .cs .go .rs .rb .php .sh "
        ".bash .zsh .bat .cmd .ps1 .sql .lua .swift .m .r .scala .dart .css .scss .less .gradle .properties .proto "
        ".diff .patch"
    ).split()
)
_MARKITDOWN_TEXT_EXTS = {".html": "html", ".htm": "html", ".xhtml": "html", ".csv": "csv", ".ipynb": "ipynb"}
_OOXML_MARKERS = (("word/document.xml", "docx"), ("ppt/presentation.xml", "pptx"), ("xl/workbook.xml", "xlsx"))
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_IMAGE_MAGICS = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")
_BINARY_MAGICS = (
    b"7z\xbc\xaf\x27\x1c",
    b"Rar!\x1a\x07",
    b"\x1f\x8b",
    b"\x7fELF",
    b"OggS",
    b"fLaC",
    b"\x1aE\xdf\xa3",
)
_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.IGNORECASE)
_BLANK_LINES_RE = re.compile(r"\n{3,}")


@dataclass(slots=True)
class DocResult:
    kind: str
    """``pdf`` / ``docx`` / ``pptx`` / ``xlsx`` / ``epub`` / ``html`` / ``csv`` / ``ipynb`` / ``text`` / ``zip``."""
    text: str
    pages: int
    """Page count of the source for PDFs; 1 otherwise."""
    truncated: bool
    scanned_pages: list[bytes] = field(default_factory=list)
    """PNG renders of the first pages when a PDF has no usable text layer (for a vision model)."""


def decode_text(data: bytes, declared: str | None = None) -> str | None:
    """Decode text bytes: BOM, declared charset, utf-8, then gb18030. ``None`` if it looks binary."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data[:8192]:
        return None
    candidates = [declared] if declared else []
    candidates += ["utf-8", "gb18030"]
    for charset in candidates:
        try:
            return data.decode(_charset_alias(charset))
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def decode_html(data: bytes, declared: str | None = None) -> str:
    """Decode an HTML document, honouring ``<meta charset>`` when the header has none."""
    if not declared and (match := _META_CHARSET_RE.search(data[:4096])):
        declared = match.group(1).decode("ascii")
    text = decode_text(data, declared)
    if text is None:
        return data.decode(_charset_alias(declared or "utf-8"), errors="replace")
    return text


def _charset_alias(charset: str) -> str:
    # GB2312/GBK pages routinely contain characters only present in their superset.
    return "gb18030" if charset.lower().replace("-", "") in {"gb2312", "gbk", "x-gbk"} else charset


def parse_document(data: bytes, filename: str, *, max_pages: int = 40, max_chars: int = 60000) -> DocResult:
    """Extract text from a document. Raises :class:`ToolError` for unsupported or broken files."""
    if not data:
        raise ToolError("文件是空的")
    ext = PurePosixPath(filename.lower().replace("\\", "/")).suffix
    if data[:1024].lstrip().startswith(b"%PDF-") or (ext == ".pdf" and b"%PDF-" in data[:1024]):
        return _parse_pdf(data, max_pages=max_pages, max_chars=max_chars)
    if data.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return _parse_zip(data, max_chars=max_chars)
    if data.startswith(_OLE_MAGIC):
        raise ToolError("暂不支持旧版 Office 格式（.doc/.xls/.ppt），请另存为 .docx/.xlsx/.pptx 后再发")
    if data.startswith(_IMAGE_MAGICS) or (data.startswith(b"RIFF") and data[8:12] == b"WEBP"):
        raise ToolError("这是图片文件，请用看图工具查看")
    if data.startswith(_BINARY_MAGICS) or data[4:8] == b"ftyp":
        raise ToolError("这是二进制文件（压缩包、程序或音视频），无法读取内容")

    text = decode_text(data)
    if text is None:
        raise ToolError("无法识别的文件格式")
    kind = _MARKITDOWN_TEXT_EXTS.get(ext)
    if kind is None and ext not in _TEXT_EXTS and text.lstrip()[:512].lower().startswith(("<!doctype html", "<html")):
        kind = "html"
    if kind is not None:
        text = _markitdown(text.encode("utf-8"), ext or ".html", charset="utf-8")
    else:
        kind = "text"
    clipped, truncated = clip_text(text, max_chars)
    return DocResult(kind=kind, text=clipped, pages=1, truncated=truncated)


def clip_text(text: str, max_chars: int) -> tuple[str, bool]:
    """Cut ``text`` to at most ``max_chars``, preferring a line boundary near the end."""
    text = _BLANK_LINES_RE.sub("\n\n", text.replace("\r\n", "\n").replace("\r", "\n")).strip()
    if len(text) <= max_chars:
        return text, False
    cut = text.rfind("\n", 0, max_chars + 1)
    if cut < max_chars * 0.8:
        cut = max_chars
    return text[:cut].rstrip(), True


def _parse_pdf(data: bytes, *, max_pages: int, max_chars: int) -> DocResult:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise ToolError("PDF 已加密或已损坏，无法解析") from exc
    try:
        total = len(pdf)
        count = min(total, max_pages)
        texts: list[str] = []
        for i in range(count):
            page = pdf[i]
            textpage = page.get_textpage()
            try:
                texts.append(textpage.get_text_range().strip())
            finally:
                textpage.close()
                page.close()
        solid = sum(len("".join(t.split())) for t in texts)
        scanned: list[bytes] = []
        if count and solid < _SCANNED_CHARS_PER_PAGE * count:
            scanned = [_render_png(pdf, i) for i in range(min(count, _SCAN_RENDER_PAGES))]
    finally:
        pdf.close()

    if total > 1:
        body = "\n\n".join(f"[第 {i} 页]\n{t}" for i, t in enumerate(texts, 1) if t)
    else:
        body = "\n".join(texts)
    clipped, truncated = clip_text(body, max_chars)
    return DocResult(
        kind="pdf", text=clipped, pages=total, truncated=truncated or total > max_pages, scanned_pages=scanned
    )


def _render_png(pdf: "pypdfium2.PdfDocument", index: int) -> bytes:
    page = pdf[index]
    try:
        image = page.render(scale=_SCAN_RENDER_SCALE).to_pil()  # pyright: ignore[reportArgumentType]
    finally:
        page.close()
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _parse_zip(data: bytes, *, max_chars: int) -> DocResult:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ToolError("压缩文件已损坏，无法解析") from exc
    with archive:
        infos = archive.infolist()
        names = {info.filename for info in infos}
        for marker, kind in _OOXML_MARKERS:
            if marker in names:
                return _office(data, kind, max_chars=max_chars)
        if "META-INF/container.xml" in names and "mimetype" in names:
            if archive.read("mimetype").strip() == b"application/epub+zip":
                return _office(data, "epub", max_chars=max_chars)
        files = [info for info in infos if not info.is_dir()]
        lines = [f"压缩包内共 {len(files)} 个文件（只列出文件名，不解压）："]
        lines += [f"- {_zip_name(info)} ({_human_size(info.file_size)})" for info in files[:_ZIP_LIST_LIMIT]]
        if len(files) > _ZIP_LIST_LIMIT:
            lines.append(f"……另有 {len(files) - _ZIP_LIST_LIMIT} 个文件未列出")
    clipped, truncated = clip_text("\n".join(lines), max_chars)
    return DocResult(kind="zip", text=clipped, pages=1, truncated=truncated or len(files) > _ZIP_LIST_LIMIT)


def _zip_name(info: zipfile.ZipInfo) -> str:
    if info.flag_bits & 0x800:
        return info.filename
    # Archives made on Chinese Windows store GBK names without the UTF-8 flag; zipfile decodes them as cp437.
    try:
        return info.filename.encode("cp437").decode("gb18030")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = size / 1024
    for unit in ("KB", "MB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _office(data: bytes, kind: str, *, max_chars: int) -> DocResult:
    clipped, truncated = clip_text(_markitdown(data, f".{kind}"), max_chars)
    return DocResult(kind=kind, text=clipped, pages=1, truncated=truncated)


@cache
def _markitdown_instance() -> "MarkItDown":
    from markitdown import MarkItDown

    return MarkItDown(enable_plugins=False)


def _markitdown(data: bytes, ext: str, charset: str | None = None) -> str:
    from markitdown import StreamInfo

    try:
        result = _markitdown_instance().convert_stream(
            io.BytesIO(data), stream_info=StreamInfo(extension=ext, charset=charset)
        )
    except Exception as exc:
        raise ToolError("文档解析失败，文件可能已损坏") from exc
    return result.text_content
