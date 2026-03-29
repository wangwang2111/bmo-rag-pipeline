"""
chunk.py
========
Splits ``DocumentRecord`` objects into retrieval-friendly ``ChunkRecord``
segments with full metadata inheritance.

Chunking strategy
-----------------
Two strategies are offered and can be selected per document or globally:

1. **SentenceSplitter** (default) — LlamaIndex's token-aware splitter that
   respects sentence boundaries.  ``chunk_size=512`` tokens with
   ``chunk_overlap=50`` tokens.  This is fast, deterministic, and works well
   for structured/technical documents (manuals, policies).

2. **SemanticSplitter** (optional) — groups sentences by embedding-space
   similarity so chunk boundaries fall at topic transitions rather than
   arbitrary token counts.  Better for long, unstructured prose but slower
   (requires embedding calls per document).  Activate by setting
   ``CHUNKING_STRATEGY=semantic`` in the environment.

Metadata preservation
---------------------
Every ``ChunkRecord`` inherits the parent document's metadata and adds:
- ``chunk_index``  — 0-based position within the document
- ``chunk_total``  — total chunks produced from that document
- ``char_start``   — character offset in the original text (approx.)
- ``chunk_text``   — convenience copy of the chunk content
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.schema import Document as LlamaDocument

from extract import DocumentRecord

load_dotenv()

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

CHUNK_SIZE: int = int(os.getenv("CHUNK_SIZE", "512"))
CHUNK_OVERLAP: int = int(os.getenv("CHUNK_OVERLAP", "50"))
CHUNKING_STRATEGY: str = os.getenv("CHUNKING_STRATEGY", "sentence").lower()
# Minimum characters a chunk must contain to be kept (noise filter)
MIN_CHUNK_CHARS: int = 30


# ── Public data contract ──────────────────────────────────────────────────────

@dataclass
class ChunkRecord:
    """A single retrievable segment derived from a ``DocumentRecord``."""

    chunk_id: str
    """
    Globally unique identifier: ``<blob_name_slug>_chunk_<index>``.
    Safe to use as a ChromaDB document ID.
    """

    blob_name: str
    """Source blob path, inherited from the parent document."""

    text: str
    """The chunk's text content."""

    chunk_index: int
    """0-based position of this chunk within its source document."""

    chunk_total: int
    """Total number of chunks produced from the parent document."""

    metadata: dict = field(default_factory=dict)
    """
    Full inherited metadata plus chunk-level fields:
    blob_name, source_type, page_count, size_bytes, file_extension,
    chunk_index, chunk_total, char_start.
    """


# ── Splitter factories ────────────────────────────────────────────────────────

def _build_sentence_splitter() -> SentenceSplitter:
    """
    Return a configured ``SentenceSplitter``.

    chunk_size and chunk_overlap are read from environment variables so they
    can be tuned without code changes.
    """
    return SentenceSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        # paragraph_separator keeps natural paragraph breaks as split hints
        paragraph_separator="\n\n",
    )


def _build_semantic_splitter(embed_model=None):
    """
    Return a ``SemanticSplitterNodeParser``.

    ``embed_model`` must be a LlamaIndex-compatible embedding model.  If
    ``None``, the function will attempt to construct a local sentence-transformer
    model so this path works without Azure OpenAI credentials.

    Notes
    -----
    The semantic splitter is considerably slower than the sentence splitter
    (one embedding call per sentence) and should only be used when chunk
    boundary quality is more important than throughput.
    """
    try:
        from llama_index.core.node_parser import SemanticSplitterNodeParser
    except ImportError as exc:
        raise ImportError(
            "SemanticSplitterNodeParser requires llama-index-core >= 0.10. "
            "pip install llama-index-core"
        ) from exc

    if embed_model is None:
        try:
            from llama_index.embeddings.huggingface import HuggingFaceEmbedding

            embed_model = HuggingFaceEmbedding(
                model_name="sentence-transformers/all-MiniLM-L6-v2"
            )
            logger.info("Semantic splitter using local HuggingFace embed model.")
        except ImportError as exc:
            raise ImportError(
                "HuggingFaceEmbedding not available. "
                "pip install llama-index-embeddings-huggingface sentence-transformers"
            ) from exc

    return SemanticSplitterNodeParser(
        embed_model=embed_model,
        breakpoint_percentile_threshold=95,  # only split at strong topic shifts
    )


# ── Core chunking logic ───────────────────────────────────────────────────────

def _make_chunk_id(blob_name: str, index: int) -> str:
    """
    Build a filesystem-safe, globally unique chunk identifier.

    We replace path separators and dots so the ID is safe as a ChromaDB key.
    """
    slug = blob_name.replace("/", "_").replace(".", "_").replace(" ", "_")
    return f"{slug}_chunk_{index:04d}"


def _llama_nodes_to_chunks(
    nodes: list,
    parent_doc: DocumentRecord,
    original_text: str,
) -> list[ChunkRecord]:
    """
    Convert a list of LlamaIndex ``TextNode`` objects into ``ChunkRecord`` list.

    Parameters
    ----------
    nodes:
        Output from a LlamaIndex node parser.
    parent_doc:
        The source ``DocumentRecord`` whose metadata is inherited.
    original_text:
        Full original text; used to compute approximate ``char_start`` offsets.
    """
    records: list[ChunkRecord] = []
    total = len(nodes)
    search_pos: int = 0  # sliding cursor for char_start approximation

    for idx, node in enumerate(nodes):
        text: str = node.get_content()

        # Skip near-empty chunks (headers stripped, page breaks, etc.)
        if len(text.strip()) < MIN_CHUNK_CHARS:
            logger.debug("Skipping near-empty chunk %d from '%s'.", idx, parent_doc.blob_name)
            continue

        # Approximate character start offset by searching forward in the original
        char_start = original_text.find(text[:50].strip(), search_pos)
        if char_start == -1:
            char_start = -1  # could not locate — set sentinel
        else:
            search_pos = char_start  # advance cursor; don't go backwards

        chunk_meta = {
            **parent_doc.metadata,
            "chunk_index": idx,
            "chunk_total": total,
            "char_start": char_start,
            "chunk_text": text,  # mirror in metadata for easy retrieval
        }

        records.append(
            ChunkRecord(
                chunk_id=_make_chunk_id(parent_doc.blob_name, idx),
                blob_name=parent_doc.blob_name,
                text=text,
                chunk_index=idx,
                chunk_total=total,
                metadata=chunk_meta,
            )
        )

    return records


# ── Public API ────────────────────────────────────────────────────────────────

def chunk_document(
    doc: DocumentRecord,
    strategy: Optional[str] = None,
    embed_model=None,
) -> list[ChunkRecord]:
    """
    Chunk a single ``DocumentRecord`` into a list of ``ChunkRecord`` objects.

    Parameters
    ----------
    doc:
        The document to chunk.
    strategy:
        ``'sentence'`` (default) or ``'semantic'``.  Falls back to the
        ``CHUNKING_STRATEGY`` environment variable if ``None``.
    embed_model:
        LlamaIndex-compatible embedding model, required only when
        ``strategy='semantic'``.

    Returns
    -------
    Ordered list of ``ChunkRecord`` objects.
    """
    effective_strategy = strategy or CHUNKING_STRATEGY

    if not doc.text.strip():
        logger.warning("'%s' has no text — skipping chunking.", doc.blob_name)
        return []

    # Wrap text in a LlamaIndex Document node so we can use the node parsers
    llama_doc = LlamaDocument(
        text=doc.text,
        metadata=doc.metadata,
        doc_id=doc.blob_name,
    )

    if effective_strategy == "semantic":
        logger.info("Chunking '%s' with SemanticSplitter.", doc.blob_name)
        splitter = _build_semantic_splitter(embed_model=embed_model)
    else:
        if effective_strategy != "sentence":
            logger.warning(
                "Unknown strategy '%s'; falling back to 'sentence'.",
                effective_strategy,
            )
        logger.info("Chunking '%s' with SentenceSplitter (size=%d, overlap=%d).",
                    doc.blob_name, CHUNK_SIZE, CHUNK_OVERLAP)
        splitter = _build_sentence_splitter()

    nodes = splitter.get_nodes_from_documents([llama_doc])
    chunks = _llama_nodes_to_chunks(nodes, doc, doc.text)

    logger.info(
        "Produced %d chunks from '%s' (strategy=%s).",
        len(chunks), doc.blob_name, effective_strategy,
    )
    return chunks


def chunk_documents(
    docs: list[DocumentRecord],
    strategy: Optional[str] = None,
    embed_model=None,
) -> list[ChunkRecord]:
    """
    Chunk a list of ``DocumentRecord`` objects.

    Parameters
    ----------
    docs:
        Documents to process.
    strategy:
        Passed through to :func:`chunk_document`.
    embed_model:
        Passed through to :func:`chunk_document` (only used for semantic strategy).

    Returns
    -------
    Flat list of all ``ChunkRecord`` objects across all documents.
    """
    all_chunks: list[ChunkRecord] = []
    for doc in docs:
        chunks = chunk_document(doc, strategy=strategy, embed_model=embed_model)
        all_chunks.extend(chunks)

    logger.info(
        "Chunking complete: %d chunks from %d documents.",
        len(all_chunks), len(docs),
    )
    return all_chunks


# ── CLI smoke-test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Build a synthetic document to verify the splitter without Azure credentials
    sample_text = (
        "This is the first sentence of a test document. "
        "It discusses device configuration and network settings. "
        "Proper chunking ensures that related sentences stay together. "
        "\n\n"
        "The second paragraph covers error handling procedures. "
        "Error 101 indicates a network timeout. "
        "The recommended action is to restart the device and check the logs. "
    ) * 20  # repeat to get enough content for multiple chunks

    synthetic = DocumentRecord(
        blob_name="test/synthetic.txt",
        source_type="txt",
        text=sample_text,
        page_count=0,
        metadata={"container": "test", "blob_name": "test/synthetic.txt",
                  "source_type": "txt", "page_count": 0, "size_bytes": len(sample_text),
                  "file_extension": ".txt"},
    )

    chunks = chunk_document(synthetic, strategy="sentence")
    print(f"\nTotal chunks: {len(chunks)}")
    for c in chunks[:3]:
        print(f"\n--- Chunk {c.chunk_index} (id={c.chunk_id}) ---")
        print(c.text[:300])
