"""
ingest.py
=========
Orchestration script for the full ETL pipeline:

    Azure Blob Storage → extract → chunk → embed → ChromaDB

Running this script end-to-end (re-)indexes all documents in the configured
container.  It is idempotent: re-running it will upsert existing chunks
rather than creating duplicates.

Usage
-----
    python ingest.py                        # full re-index
    python ingest.py --blobs manuals/deviceA.pdf troubleshooting/error101.md
    python ingest.py --reset                # drop collection then re-index
    python ingest.py --strategy semantic    # use semantic chunker

Pipeline stages
---------------
1. ``extract``  — download and parse documents from Azure Blob Storage
2. ``chunk``    — split into retrieval-friendly segments
3. ``embed``    — generate dense vector embeddings
4. ``index``    — upsert into ChromaDB persistent collection
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Optional

from dotenv import load_dotenv

# Ensure src/ is on the Python path when called from the project root
sys.path.insert(0, os.path.dirname(__file__))

from chunk import ChunkRecord, chunk_documents
from embed import EmbeddedChunk, embed_chunks, _build_embedder
from extract import DocumentRecord, extract_all_documents, _build_container_client
from index import (
    delete_collection,
    get_chroma_client,
    get_collection_stats,
    get_or_create_collection,
    index_chunks,
)

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)


# ── Pipeline stages ───────────────────────────────────────────────────────────

def stage_extract(
    blob_names: Optional[list[str]] = None,
) -> list[DocumentRecord]:
    """
    Stage 1: Connect to Azure Blob Storage and extract all documents.

    Parameters
    ----------
    blob_names:
        Optional list of specific blobs to ingest.  If ``None``, all blobs
        in the container are processed.

    Returns
    -------
    List of ``DocumentRecord`` objects with extracted text and metadata.
    """
    logger.info("=== STAGE 1: EXTRACT ===")
    container_client = _build_container_client()
    docs = extract_all_documents(container_client, blob_names=blob_names)
    logger.info("Extracted %d documents.", len(docs))
    return docs


def stage_chunk(
    docs: list[DocumentRecord],
    strategy: str = "sentence",
) -> list[ChunkRecord]:
    """
    Stage 2: Chunk extracted documents into retrieval segments.

    Parameters
    ----------
    docs:
        Documents to chunk.
    strategy:
        ``'sentence'`` (default) or ``'semantic'``.

    Returns
    -------
    Flat list of ``ChunkRecord`` objects.
    """
    logger.info("=== STAGE 2: CHUNK (strategy=%s) ===", strategy)
    chunks = chunk_documents(docs, strategy=strategy)
    logger.info("Produced %d chunks from %d documents.", len(chunks), len(docs))
    return chunks


def stage_embed(chunks: list[ChunkRecord]) -> list[EmbeddedChunk]:
    """
    Stage 3: Generate embeddings for each chunk.

    Parameters
    ----------
    chunks:
        Chunks to embed.

    Returns
    -------
    List of ``EmbeddedChunk`` objects (same order as input).
    """
    logger.info("=== STAGE 3: EMBED ===")
    embedder = _build_embedder()
    embedded = embed_chunks(chunks, embedder=embedder)
    logger.info(
        "Generated %d embeddings (dim=%d).",
        len(embedded),
        len(embedded[0].embedding) if embedded else 0,
    )
    return embedded


def stage_index(embedded: list[EmbeddedChunk]) -> dict:
    """
    Stage 4: Upsert embedded chunks into ChromaDB.

    Parameters
    ----------
    embedded:
        Embedded chunks to persist.

    Returns
    -------
    Statistics dict from :func:`get_collection_stats`.
    """
    logger.info("=== STAGE 4: INDEX ===")
    client = get_chroma_client()
    collection = get_or_create_collection(client=client)
    index_chunks(embedded, collection=collection)
    stats = get_collection_stats(collection)
    logger.info("Index stats: %s", stats)
    return stats


# ── Full pipeline ─────────────────────────────────────────────────────────────

def run_pipeline(
    blob_names: Optional[list[str]] = None,
    strategy: str = "sentence",
    reset: bool = False,
) -> dict:
    """
    Run the full extract → chunk → embed → index pipeline.

    Parameters
    ----------
    blob_names:
        Optional list of blobs to process (``None`` = all).
    strategy:
        Chunking strategy: ``'sentence'`` or ``'semantic'``.
    reset:
        If ``True``, drop the existing ChromaDB collection before indexing.

    Returns
    -------
    Pipeline summary dict with timing and statistics.
    """
    pipeline_start = time.perf_counter()
    summary: dict = {
        "blob_names": blob_names,
        "strategy": strategy,
        "reset": reset,
        "stages": {},
    }

    if reset:
        logger.warning("RESET requested — dropping existing collection.")
        delete_collection()

    # ── Stage 1: Extract ──────────────────────────────────────────────────────
    t0 = time.perf_counter()
    docs = stage_extract(blob_names=blob_names)
    summary["stages"]["extract"] = {
        "docs_extracted": len(docs),
        "duration_s": round(time.perf_counter() - t0, 2),
    }
    if not docs:
        logger.error("No documents extracted. Aborting pipeline.")
        summary["status"] = "failed_no_documents"
        return summary

    # ── Stage 2: Chunk ────────────────────────────────────────────────────────
    t0 = time.perf_counter()
    chunks = stage_chunk(docs, strategy=strategy)
    summary["stages"]["chunk"] = {
        "chunks_produced": len(chunks),
        "duration_s": round(time.perf_counter() - t0, 2),
    }
    if not chunks:
        logger.error("No chunks produced. Aborting pipeline.")
        summary["status"] = "failed_no_chunks"
        return summary

    # ── Stage 3: Embed ────────────────────────────────────────────────────────
    t0 = time.perf_counter()
    embedded = stage_embed(chunks)
    summary["stages"]["embed"] = {
        "embeddings_generated": len(embedded),
        "embedding_dim": len(embedded[0].embedding) if embedded else 0,
        "duration_s": round(time.perf_counter() - t0, 2),
    }

    # ── Stage 4: Index ────────────────────────────────────────────────────────
    t0 = time.perf_counter()
    stats = stage_index(embedded)
    summary["stages"]["index"] = {
        **stats,
        "duration_s": round(time.perf_counter() - t0, 2),
    }

    total_duration = round(time.perf_counter() - pipeline_start, 2)
    summary["total_duration_s"] = total_duration
    summary["status"] = "success"

    logger.info(
        "Pipeline complete in %.2fs. %d docs → %d chunks → %d embeddings indexed.",
        total_duration, len(docs), len(chunks), len(embedded),
    )
    return summary


# ── CLI entrypoint ────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the BMO RAG ingestion pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Ingest all blobs in the configured container
  python ingest.py

  # Ingest specific blobs
  python ingest.py --blobs manuals/deviceA.pdf troubleshooting/error101.md

  # Drop existing collection and re-index with semantic chunking
  python ingest.py --reset --strategy semantic
        """,
    )
    parser.add_argument(
        "--blobs",
        nargs="*",
        metavar="BLOB",
        help="Specific blob names to ingest (default: all blobs in container).",
    )
    parser.add_argument(
        "--strategy",
        choices=["sentence", "semantic"],
        default="sentence",
        help="Chunking strategy (default: sentence).",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop the existing ChromaDB collection before indexing.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    import json

    args = _parse_args()
    summary = run_pipeline(
        blob_names=args.blobs or None,
        strategy=args.strategy,
        reset=args.reset,
    )
    print("\n" + "=" * 60)
    print("PIPELINE SUMMARY")
    print("=" * 60)
    print(json.dumps(summary, indent=2))
