import io
import zipfile

import pytest
from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches

from hermes.document_loader import SUPPORTED_EXTENSIONS, extract_document_text


def _docx_bytes():
    document = Document()
    document.add_paragraph("Returns are accepted within three days.")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Delivery"
    table.cell(0, 1).text = "Free over KES 5,000"
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


def _xlsx_bytes():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Products"
    sheet.append(["Product", "Price"])
    sheet.append(["Maize flour", 180])
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def _pptx_bytes():
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    shape = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
    shape.text = "Returns within three days"
    stream = io.BytesIO()
    presentation.save(stream)
    return stream.getvalue()


def _pdf_bytes():
    content = b"BT /F1 12 Tf 10 100 Td (Returns within three days) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(output)


@pytest.mark.parametrize(
    ("filename", "content_factory", "expected"),
    [
        pytest.param("policy.pdf", _pdf_bytes, "Returns within three days", id="pdf"),
        pytest.param("policy.docx", _docx_bytes, "Returns are accepted", id="word"),
        pytest.param("catalog.xlsx", _xlsx_bytes, "Maize flour | 180", id="spreadsheet"),
        pytest.param("guide.pptx", _pptx_bytes, "Returns within three days", id="presentation"),
        pytest.param("policy.txt", lambda: b"Returns within three days.", "Returns within three days", id="text"),
        pytest.param("policy.md", lambda: b"# Returns\nThree days.", "Three days", id="markdown"),
        pytest.param(
            "catalog.csv",
            lambda: b"Product,Price\nMaize flour,180\n",
            "Maize flour | 180",
            id="csv",
        ),
        pytest.param("faq.json", lambda: b'{"hours":"9 to 5"}', "9 to 5", id="json"),
        pytest.param(
            "faq.html",
            lambda: b"<h1>Hours</h1><p>9 to 5</p><script>secret()</script>",
            "9 to 5",
            id="html",
        ),
        pytest.param("faq.xml", lambda: b"<hours><open>9</open></hours>", "9", id="xml"),
        pytest.param("notes.yaml", lambda: b"hours: 9 to 5", "hours: 9 to 5", id="yaml"),
        pytest.param("mail.eml", lambda: b"Subject: Store hours\n\nWe open at 9.", "We open at 9.", id="email"),
    ],
)
def test_extract_supported_documents(filename, content_factory, expected):
    display_name, extracted = extract_document_text(filename, content_factory())

    assert display_name == filename
    assert expected in extracted
    assert "secret()" not in extracted


def test_extract_rtf_and_windows_file_name():
    display_name, extracted = extract_document_text(
        r"C:\uploads\policy.rtf",
        br"{\rtf1\ansi Returns \b accepted\b0 in three days.}",
    )

    assert display_name == "policy.rtf"
    assert "Returns" in extracted
    assert "three days" in extracted


def test_reject_unsupported_and_empty_files():
    with pytest.raises(ValueError, match="Unsupported file type"):
        extract_document_text("archive.zip", b"data")
    with pytest.raises(ValueError, match="empty"):
        extract_document_text("empty.txt", b"")
    with pytest.raises(ValueError, match="200 characters"):
        extract_document_text(f"{'x' * 201}.txt", b"text")


def test_reject_scanned_pdfs_without_ocr():
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    stream = io.BytesIO()
    writer.write(stream)

    with pytest.raises(ValueError, match="need OCR"):
        extract_document_text("scanned.pdf", stream.getvalue())


def test_reject_oversized_extracted_text():
    with pytest.raises(ValueError, match="character limit"):
        extract_document_text("large.txt", b"x" * 500_001)


def test_reject_unsafe_office_archive():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"a" * 2_000_000)

    with pytest.raises(ValueError, match="compression ratio"):
        extract_document_text("unsafe.docx", stream.getvalue())


def test_supported_extension_allowlist_includes_common_uploads():
    assert {
        ".pdf",
        ".docx",
        ".docm",
        ".xlsx",
        ".xlsm",
        ".pptx",
        ".pptm",
        ".txt",
        ".md",
        ".csv",
        ".json",
        ".html",
        ".xml",
        ".yaml",
        ".eml",
    }.issubset(SUPPORTED_EXTENSIONS)
