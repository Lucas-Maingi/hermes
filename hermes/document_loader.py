"""Safe text extraction for documents indexed by the tenant knowledge base."""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from email import policy
from email.errors import MessageError
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import PureWindowsPath

MAX_DOCUMENT_TEXT_CHARS = 500_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 100_000_000
MAX_ARCHIVE_MEMBERS = 10_000
MAX_PDF_PAGES = 250
MAX_SPREADSHEET_CELLS = 100_000
SUPPORTED_EXTENSIONS = frozenset(
    {
        ".csv",
        ".docm",
        ".docx",
        ".eml",
        ".htm",
        ".html",
        ".json",
        ".log",
        ".md",
        ".pptm",
        ".pptx",
        ".pdf",
        ".rtf",
        ".txt",
        ".xlsm",
        ".xlsx",
        ".xml",
        ".yaml",
        ".yml",
    }
)


class _TextOnlyHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._ignored_depth += 1
        elif tag.lower() in {"p", "br", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag.lower() in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _decode_text(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return content.decode("cp1252")
        except UnicodeDecodeError as exc:
            raise ValueError("This file is not valid UTF-8 or Windows text.") from exc


def _validate_archive(content: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("This office document is invalid or damaged.") from exc
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise ValueError("This office document contains too many internal files.")
    if sum(member.file_size for member in members) > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        raise ValueError("This office document is too large to safely extract.")
    for member in members:
        if member.file_size and member.file_size / max(1, member.compress_size) > 1000:
            raise ValueError("This office document has an unsafe compression ratio.")


def _extract_plain_text(extension: str, content: bytes) -> str:
    text = _decode_text(content).replace("\x00", "")
    if extension == ".json":
        try:
            return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("This JSON file is invalid.") from exc
    if extension in {".html", ".htm", ".xml"}:
        parser = _TextOnlyHTMLParser()
        try:
            parser.feed(text)
            parser.close()
        except Exception as exc:
            raise ValueError("This HTML/XML file is invalid.") from exc
        return "".join(parser.parts)
    if extension == ".csv":
        try:
            rows = csv.reader(io.StringIO(text))
            return "\n".join(" | ".join(cell.strip() for cell in row) for row in rows)
        except csv.Error as exc:
            raise ValueError("This CSV file is invalid.") from exc
    if extension == ".rtf":
        text = re.sub(r"\\'[0-9a-fA-F]{2}", " ", text)
        text = re.sub(r"\\(?:par|line|tab)\b", "\n", text)
        text = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", text)
        return re.sub(r"[{}]", "", text)
    return text


def _extract_pdf(content: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError(f"PDFs are limited to {MAX_PDF_PAGES} pages per upload.")
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("This PDF is invalid, damaged, or encrypted.") from exc


def _extract_word(content: bytes) -> str:
    _validate_archive(content)
    from docx import Document

    try:
        document = Document(io.BytesIO(content))
    except Exception as exc:
        raise ValueError("This Word document is invalid or damaged.") from exc
    parts = [paragraph.text for paragraph in document.paragraphs]
    parts.extend(
        " | ".join(cell.text for row in table.rows for cell in row.cells)
        for table in document.tables
    )
    return "\n".join(parts)


def _extract_spreadsheet(content: bytes) -> str:
    _validate_archive(content)
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        parts: list[str] = []
        cell_count = 0
        for sheet in workbook.worksheets:
            parts.append(f"Sheet: {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                values = [str(value).strip() if value is not None else "" for value in row]
                cell_count += len(values)
                if cell_count > MAX_SPREADSHEET_CELLS:
                    raise ValueError(
                        f"Spreadsheets are limited to {MAX_SPREADSHEET_CELLS} cells per upload."
                    )
                if any(values):
                    parts.append(" | ".join(values))
        workbook.close()
        return "\n".join(parts)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("This spreadsheet is invalid or damaged.") from exc


def _extract_presentation(content: bytes) -> str:
    _validate_archive(content)
    from pptx import Presentation

    try:
        presentation = Presentation(io.BytesIO(content))
    except Exception as exc:
        raise ValueError("This PowerPoint file is invalid or damaged.") from exc
    parts: list[str] = []
    for slide_number, slide in enumerate(presentation.slides, start=1):
        parts.append(f"Slide {slide_number}")
        for shape in slide.shapes:
            if shape.has_text_frame:
                parts.append(shape.text)
            if shape.has_table:
                parts.extend(
                    " | ".join(cell.text for cell in row.cells)
                    for row in shape.table.rows
                )
    return "\n".join(parts)


def _extract_email(content: bytes) -> str:
    try:
        message = BytesParser(policy=policy.default).parsebytes(content)
    except (MessageError, ValueError) as exc:
        raise ValueError("This email file is invalid.") from exc
    parts = [
        f"Subject: {message.get('subject', '')}",
        f"From: {message.get('from', '')}",
    ]
    body = message.get_body(preferencelist=("plain",))
    if body is not None:
        parts.append(body.get_content())
    return "\n".join(parts)


def extract_document_text(filename: str, content: bytes) -> tuple[str, str]:
    """Return sanitized display name and extracted text for a supported upload."""
    display_name = re.sub(
        r"[\x00-\x1f\x7f]",
        "",
        PureWindowsPath(filename.replace("/", "\\")).name,
    ).strip()
    if not display_name or display_name in {".", ".."}:
        raise ValueError("Choose a file with a valid filename.")
    if len(display_name) > 200:
        raise ValueError("Filename must be 200 characters or fewer.")
    extension = PureWindowsPath(display_name).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise ValueError(f"Unsupported file type '{extension or '(no extension)'}'. Supported types: {supported}.")
    if not content:
        raise ValueError("The uploaded file is empty.")

    if extension == ".pdf":
        text = _extract_pdf(content)
    elif extension in {".docx", ".docm"}:
        text = _extract_word(content)
    elif extension in {".xlsx", ".xlsm"}:
        text = _extract_spreadsheet(content)
    elif extension in {".pptx", ".pptm"}:
        text = _extract_presentation(content)
    elif extension == ".eml":
        text = _extract_email(content)
    else:
        text = _extract_plain_text(extension, content)

    text = text.strip()
    if not text:
        if extension == ".pdf":
            raise ValueError("No selectable text was found. Scanned/image-only PDFs need OCR before upload.")
        raise ValueError("No readable text was found in this file.")
    if len(text) > MAX_DOCUMENT_TEXT_CHARS:
        raise ValueError(f"Extracted text exceeds the {MAX_DOCUMENT_TEXT_CHARS:,}-character limit.")
    return display_name, text
