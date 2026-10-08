import io
import zipfile

import pypdfium2 as pdfium
import pytest

from yggdrasil.tools.docs import parse_document
from yggdrasil.tools.errors import ToolError


def _text_pdf(*lines: str) -> bytes:
    """Minimal single-page PDF with a Helvetica text layer."""
    ops = " ".join(f"({line}) Tj 0 -22 Td" for line in lines)
    content = f"BT /F1 18 Tf 20 120 Td {ops} ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 144] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def _blank_pdf(pages: int) -> bytes:
    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(300, 200)
    buf = io.BytesIO()
    pdf.save(buf)
    pdf.close()
    return buf.getvalue()


def _docx(*paragraphs: str) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>'
        ),
        "word/document.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{body}</w:body></w:document>"
        ),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def test_pdf_text_layer_is_extracted() -> None:
    result = parse_document(_text_pdf("Hello Yggdrasil", "Second line"), "paper.pdf")
    assert result.kind == "pdf"
    assert "Hello Yggdrasil" in result.text
    assert "Second line" in result.text
    assert result.pages == 1
    assert result.scanned_pages == []


def test_scanned_pdf_renders_first_pages_for_vision() -> None:
    result = parse_document(_blank_pdf(6), "scan.pdf")
    assert result.pages == 6
    assert len(result.scanned_pages) == 4
    assert all(png.startswith(b"\x89PNG\r\n\x1a\n") for png in result.scanned_pages)


def test_scanned_render_respects_max_pages() -> None:
    result = parse_document(_blank_pdf(3), "scan.pdf", max_pages=2)
    assert len(result.scanned_pages) == 2
    assert result.truncated


def test_pdf_detected_by_magic_regardless_of_name() -> None:
    assert parse_document(_text_pdf("magic"), "download").kind == "pdf"


def test_corrupt_pdf_is_tool_error() -> None:
    with pytest.raises(ToolError):
        parse_document(b"%PDF-1.4\ngarbage", "x.pdf")


def test_docx_via_markitdown() -> None:
    result = parse_document(_docx("第一段内容", "Second paragraph"), "report.docx")
    assert result.kind == "docx"
    assert "第一段内容" in result.text
    assert "Second paragraph" in result.text


def test_zip_lists_members_without_extracting() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "secret body")
        zf.writestr("资料/数据.csv", "a,b\n1,2")
    result = parse_document(buf.getvalue(), "bundle.zip")
    assert result.kind == "zip"
    assert "readme.txt" in result.text
    assert "资料/数据.csv" in result.text
    assert "secret body" not in result.text


def test_gbk_text_is_decoded() -> None:
    result = parse_document("你好，世界。这是一段 GBK 文本。".encode("gbk"), "note.txt")
    assert result.kind == "text"
    assert result.text == "你好，世界。这是一段 GBK 文本。"


def test_csv_becomes_markdown_table() -> None:
    result = parse_document("名字,数量\n苹果,3\n".encode(), "items.csv")
    assert result.kind == "csv"
    assert "| 名字 | 数量 |" in result.text


def test_text_is_clipped_on_line_boundary() -> None:
    data = "\n".join(f"line {i:04d}" for i in range(1000)).encode()
    result = parse_document(data, "log.txt", max_chars=100)
    assert result.truncated
    assert len(result.text) <= 100
    assert result.text.endswith("line 0009")  # 10 lines x 9 chars + 9 newlines = 99


@pytest.mark.parametrize(
    ("data", "name"),
    [
        (b"", "empty.txt"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "pic.png"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32, "old.doc"),
        (bytes(range(256)) * 4, "blob.bin"),
        (b"PK\x03\x04broken", "broken.zip"),
    ],
)
def test_unsupported_inputs_raise_tool_error(data: bytes, name: str) -> None:
    with pytest.raises(ToolError):
        parse_document(data, name)
