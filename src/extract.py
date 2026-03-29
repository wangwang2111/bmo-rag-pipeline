"""
extract.py
==========
Connects to Azure Blob Storage and extracts text + metadata from:
  - Digital PDFs  (PyMuPDF / fitz)
  - Scanned PDFs  (pdf2image + pytesseract OCR)
  - Markdown files
  - Plain-text files

Each document is returned as a ``DocumentRecord`` dataclass so downstream
modules receive a stable, typed interface regardless of source format.

Design decisions
----------------
* We detect *scanned* PDFs heuristically: if PyMuPDF extracts fewer than
  ``MIN_CHARS_PER_PAGE`` characters per page on average we fall back to OCR.
  This keeps the fast digital path for native PDFs and only pays the OCR cost
  when necessary.
* All Azure credentials come from environment variables (never hardcoded).
* The module is independently runnable via ``python extract.py`` for quick
  smoke-testing against a live container.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
from azure.storage.blob import BlobServiceClient, ContainerClient
from dotenv import load_dotenv

load_dotenv()

_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_LOG_DIR / "extract.log", mode="a", encoding="utf-8"),
    ],
)

# ── Heuristic threshold ───────────────────────────────────────────────────────
# If a PDF page yields fewer than this many characters via PyMuPDF text
# extraction, we consider the page to be scanned (image-only) and apply OCR.
MIN_CHARS_PER_PAGE: int = 50


# ── Public data contract ──────────────────────────────────────────────────────

@dataclass
class DocumentRecord:
    """Holds all extracted content and metadata for one source document."""

    blob_name: str
    """Original path in the container, e.g. 'manuals/deviceA.pdf'."""

    source_type: str
    """One of: 'pdf_digital', 'pdf_scanned', 'markdown', 'txt'."""

    text: str
    """Full extracted / OCR'd text (Unicode)."""

    page_count: int = 0
    """Number of pages (PDFs only; 0 for text/markdown)."""

    metadata: dict = field(default_factory=dict)
    """
    Arbitrary key-value pairs surfaced from the document or blob service:
    container, blob_name, source_type, page_count, size_bytes, content_type.
    """


# ── Azure helpers ─────────────────────────────────────────────────────────────

def _build_container_client() -> ContainerClient:
    """
    Build an Azure Blob Storage ``ContainerClient`` from environment variables.

    Supports two auth styles (in priority order):
    1. Full connection string  (``AZURE_STORAGE_CONNECTION_STRING``)
    2. Account name + key      (``AZURE_STORAGE_ACCOUNT_NAME`` +
                                ``AZURE_STORAGE_ACCOUNT_KEY``)
    """
    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    container = os.getenv("AZURE_STORAGE_CONTAINER_NAME", "documents")

    if conn_str:
        logger.debug("Using connection-string auth for Azure Blob Storage.")
        service = BlobServiceClient.from_connection_string(conn_str)
    else:
        account_name = os.environ["AZURE_STORAGE_ACCOUNT_NAME"]
        account_key = os.environ["AZURE_STORAGE_ACCOUNT_KEY"]
        url = f"https://{account_name}.blob.core.windows.net"
        logger.debug("Using account-key auth for Azure Blob Storage: %s", url)
        service = BlobServiceClient(account_url=url, credential=account_key)

    return service.get_container_client(container)


def list_blobs(container_client: ContainerClient) -> list[str]:
    """Return all blob names in the container."""
    names = [b.name for b in container_client.list_blobs()]
    logger.info("Found %d blobs in container '%s'.", len(names),
                container_client.container_name)
    return names


def download_blob(container_client: ContainerClient, blob_name: str) -> bytes:
    """Download a blob and return its raw bytes."""
    logger.debug("Downloading blob: %s", blob_name)
    blob_client = container_client.get_blob_client(blob_name)
    data: bytes = blob_client.download_blob().readall()
    logger.debug("Downloaded %d bytes for '%s'.", len(data), blob_name)
    return data


# ── Format-specific extractors ────────────────────────────────────────────────

def _extract_digital_pdf(data: bytes) -> tuple[str, int]:
    """
    Extract text from a native (digital) PDF using PyMuPDF.

    Returns (full_text, page_count).
    """
    doc = fitz.open(stream=data, filetype="pdf")
    pages: list[str] = []
    for page in doc:
        pages.append(page.get_text("text"))  # type: ignore[arg-type]
    doc.close()
    return "\n\n".join(pages), len(pages)


def _extract_scanned_pdf(data: bytes) -> tuple[str, int]:
    """
    OCR a scanned PDF using pdf2image + pytesseract.

    Each page is converted to a PIL Image at 300 DPI and fed to Tesseract.
    Returns (full_text, page_count).

    Raises ``ImportError`` if pdf2image / pytesseract are not installed.
    """
    try:
        import pytesseract
        from pdf2image import convert_from_bytes
    except ImportError as exc:
        raise ImportError(
            "OCR dependencies missing. Install pdf2image and pytesseract."
        ) from exc

    logger.info("Running OCR pipeline on scanned PDF (%d bytes).", len(data))
    images = convert_from_bytes(data, dpi=300)
    page_texts: list[str] = []
    for i, img in enumerate(images, start=1):
        logger.debug("  OCR page %d / %d …", i, len(images))
        page_texts.append(pytesseract.image_to_string(img, lang="eng"))
    return "\n\n".join(page_texts), len(images)


def _is_scanned_pdf(data: bytes) -> bool:
    """
    Heuristically decide whether a PDF is scanned (image-only).

    Strategy: open with PyMuPDF, compute average characters per page.
    If below ``MIN_CHARS_PER_PAGE``, treat as scanned.
    """
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        if doc.page_count == 0:
            doc.close()
            return False
        total_chars = sum(len(page.get_text("text")) for page in doc)  # type: ignore[arg-type]
        avg = total_chars / doc.page_count
        doc.close()
        logger.debug("Avg chars/page = %.1f (threshold=%d).", avg, MIN_CHARS_PER_PAGE)
        return avg < MIN_CHARS_PER_PAGE
    except Exception as exc:
        logger.warning("Could not inspect PDF for scan detection: %s", exc)
        return False


def _extract_pdf(data: bytes, blob_name: str) -> tuple[str, int, str]:
    """
    Route PDF to the correct extractor.

    Returns (text, page_count, source_type).
    """
    if _is_scanned_pdf(data):
        logger.info("'%s' detected as scanned PDF — using OCR.", blob_name)
        text, pages = _extract_scanned_pdf(data)
        return text, pages, "pdf_scanned"
    else:
        logger.info("'%s' detected as digital PDF — using PyMuPDF.", blob_name)
        text, pages = _extract_digital_pdf(data)
        return text, pages, "pdf_digital"


def _extract_markdown(data: bytes) -> str:
    """
    Convert Markdown bytes to plain text by stripping HTML tags after rendering.

    Falls back to raw UTF-8 decode if markdown/bs4 are unavailable.
    """
    raw = data.decode("utf-8", errors="replace")
    try:
        import markdown as md
        from bs4 import BeautifulSoup

        html = md.markdown(raw)
        soup = BeautifulSoup(html, "html.parser")
        return soup.get_text(separator="\n")
    except ImportError:
        logger.warning("markdown/bs4 not installed — returning raw Markdown text.")
        return raw


def _extract_txt(data: bytes) -> str:
    """Decode plain-text bytes to Unicode, replacing undecodable bytes."""
    return data.decode("utf-8", errors="replace")


# ── Public API ────────────────────────────────────────────────────────────────

def extract_document(
    container_client: ContainerClient,
    blob_name: str,
) -> Optional[DocumentRecord]:
    """
    Download and extract a single blob from Azure Blob Storage.

    Parameters
    ----------
    container_client:
        An authenticated Azure ``ContainerClient``.
    blob_name:
        The full blob path within the container.

    Returns
    -------
    ``DocumentRecord`` on success, ``None`` if the file type is unsupported or
    extraction fails.
    """
    try:
        data = download_blob(container_client, blob_name)
    except Exception as exc:
        logger.error("Failed to download '%s': %s", blob_name, exc)
        return None

    ext = Path(blob_name).suffix.lower()
    text: str = ""
    page_count: int = 0
    source_type: str = "unknown"

    try:
        if ext == ".pdf":
            text, page_count, source_type = _extract_pdf(data, blob_name)
        elif ext in {".md", ".markdown"}:
            text = _extract_markdown(data)
            source_type = "markdown"
        elif ext == ".txt":
            text = _extract_txt(data)
            source_type = "txt"
        else:
            logger.warning("Unsupported file type '%s' for blob '%s'. Skipping.", ext, blob_name)
            return None
    except Exception as exc:
        logger.error("Extraction failed for '%s': %s", blob_name, exc, exc_info=True)
        return None

    # Trim excessive whitespace while preserving paragraph breaks
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = text.strip()

    if not text:
        logger.warning("'%s' produced empty text after extraction.", blob_name)

    metadata = {
        "container": container_client.container_name,
        "blob_name": blob_name,
        "source_type": source_type,
        "page_count": page_count,
        "size_bytes": len(data),
        "file_extension": ext,
    }

    logger.info(
        "Extracted '%s' (%s): %d chars, %d pages.",
        blob_name, source_type, len(text), page_count,
    )
    return DocumentRecord(
        blob_name=blob_name,
        source_type=source_type,
        text=text,
        page_count=page_count,
        metadata=metadata,
    )


def extract_all_documents(
    container_client: Optional[ContainerClient] = None,
    blob_names: Optional[list[str]] = None,
) -> list[DocumentRecord]:
    """
    Extract all (or a filtered subset of) documents from the container.

    Parameters
    ----------
    container_client:
        If ``None``, a client is constructed from environment variables.
    blob_names:
        Optional explicit list of blob names to process. If ``None``, all blobs
        in the container are processed.

    Returns
    -------
    List of successfully extracted ``DocumentRecord`` objects.
    """
    if container_client is None:
        container_client = _build_container_client()

    if blob_names is None:
        blob_names = list_blobs(container_client)

    records: list[DocumentRecord] = []
    for name in blob_names:
        record = extract_document(container_client, name)
        if record is not None:
            records.append(record)

    logger.info(
        "Extraction complete: %d / %d documents succeeded.",
        len(records), len(blob_names),
    )
    return records


# ── CLI smoke-test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    records = extract_all_documents()
    for rec in records:
        preview = rec.text[:200].replace("\n", " ")
        print(f"\n{'=' * 60}")
        print(f"Blob       : {rec.blob_name}")
        print(f"Type       : {rec.source_type}")
        print(f"Pages      : {rec.page_count}")
        print(f"Chars      : {len(rec.text):,}")
        print(f"Preview    : {preview} …")
        print(f"Metadata   : {json.dumps(rec.metadata, indent=2)}")
