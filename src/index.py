"""
index.py
========
Persists ``EmbeddedChunk`` objects into a **ChromaDB** vector store with
full metadata filtering support.

ChromaDB is chosen because it:
* Is fully embedded (no separate server required for development)
* Supports persistent storage on disk out-of-the-box
* Provides both vector similarity search and metadata filtering in one query
* Has a clean Python API

Schema
------
Collection name: ``bmo_rag_chunks``

Each document stored in ChromaDB has:
  - **id**        : ``chunk_id`` (globally unique)
  - **embedding** : dense vector from Azure OpenAI or sentence-transformers
  - **document**  : raw chunk text (for BM25 and caption extraction)
  - **metadata**  : all fields from ``EmbeddedChunk.metadata`` with the
                    addition of ``embedding_model``.

Design decisions
----------------
* **Upsert semantics**: we always use ``upsert`` so the pipeline is
  idempotent — re-running ingest on the same blobs won't create duplicates.
* **Batch upserts**: large collections are written in batches of
  ``INDEX_BATCH_SIZE`` to avoid memory spikes.
* **Metadata flattening**: ChromaDB requires that metadata values are
  str/int/float/bool.  Nested dicts or lists are JSON-serialised to strings.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import chromadb
from chromadb.config import Settings
from dotenv import load_dotenv

from embed import EmbeddedChunk

load_dotenv()

logger = logging.getLogger(__name__)

# Suppress ChromaDB's broken posthog telemetry (version mismatch in 0.4.24)
logging.getLogger("chromadb.telemetry.product.posthog").setLevel(logging.CRITICAL)

# ── Configuration ─────────────────────────────────────────────────────────────

CHROMA_PERSIST_DIR: str = os.getenv("CHROMA_PERSIST_DIR", "./chroma_db")
COLLECTION_NAME: str = "bmo_rag_chunks"
INDEX_BATCH_SIZE: int = 100  # chunks per ChromaDB upsert call


# ── ChromaDB client factory ───────────────────────────────────────────────────

def get_chroma_client(persist_dir: Optional[str] = None) -> chromadb.PersistentClient:
    """
    Return a persistent ChromaDB client.

    Parameters
    ----------
    persist_dir:
        Path to the directory where ChromaDB stores its data.
        Defaults to ``CHROMA_PERSIST_DIR`` env var (or ``./chroma_db``).
    """
    path = persist_dir or CHROMA_PERSIST_DIR
    os.makedirs(path, exist_ok=True)
    logger.debug("ChromaDB persistent path: %s", path)
    client = chromadb.PersistentClient(
        path=path,
        settings=Settings(anonymized_telemetry=False),
    )
    return client


def get_or_create_collection(
    client: Optional[chromadb.PersistentClient] = None,
    collection_name: str = COLLECTION_NAME,
) -> chromadb.Collection:
    """
    Return (or create) the ChromaDB collection for RAG chunks.

    Uses cosine distance metric which is appropriate for L2-normalised
    embeddings produced by both Azure OpenAI and the local fallback.

    Parameters
    ----------
    client:
        Optional pre-built ChromaDB client.  Built automatically if ``None``.
    collection_name:
        Name of the ChromaDB collection.
    """
    if client is None:
        client = get_chroma_client()

    collection = client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},  # cosine similarity for retrieval
    )
    logger.info(
        "Collection '%s' ready (%d existing documents).",
        collection_name, collection.count(),
    )
    return collection


# ── Metadata sanitisation ─────────────────────────────────────────────────────

def _sanitise_metadata(meta: dict) -> dict:
    """
    Flatten metadata to ChromaDB-compatible scalar types.

    ChromaDB only accepts ``str``, ``int``, ``float``, and ``bool`` as
    metadata values.  Any other type is JSON-serialised to a string so it can
    be round-tripped later with ``json.loads``.
    """
    clean: dict = {}
    for key, value in meta.items():
        if isinstance(value, (str, int, float, bool)):
            clean[key] = value
        else:
            # Serialise non-scalar values (lists, dicts, None, etc.)
            clean[key] = json.dumps(value)
    return clean


# ── Public API ────────────────────────────────────────────────────────────────

def index_chunks(
    chunks: list[EmbeddedChunk],
    collection: Optional[chromadb.Collection] = None,
    batch_size: int = INDEX_BATCH_SIZE,
) -> chromadb.Collection:
    """
    Upsert a list of ``EmbeddedChunk`` objects into ChromaDB.

    The operation is idempotent: running it twice with the same chunks will
    not create duplicates (``chunk_id`` is used as the document ID).

    Parameters
    ----------
    chunks:
        Embedded chunks to persist.
    collection:
        Optional pre-fetched ChromaDB collection.  Created automatically if
        ``None``.
    batch_size:
        Number of chunks per upsert call.

    Returns
    -------
    The ChromaDB collection (useful for chaining / further queries).
    """
    if not chunks:
        logger.warning("index_chunks called with empty list.")
        if collection is None:
            collection = get_or_create_collection()
        return collection

    if collection is None:
        collection = get_or_create_collection()

    total = len(chunks)
    upserted = 0

    for batch_start in range(0, total, batch_size):
        batch = chunks[batch_start: batch_start + batch_size]

        ids = [c.chunk_id for c in batch]
        embeddings = [c.embedding for c in batch]
        documents = [c.text for c in batch]
        metadatas = [_sanitise_metadata(c.metadata) for c in batch]

        collection.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )
        upserted += len(batch)
        logger.debug(
            "Upserted batch %d–%d / %d into '%s'.",
            batch_start + 1, upserted, total, collection.name,
        )

    logger.info(
        "Indexed %d chunks into collection '%s' (total in collection: %d).",
        total, collection.name, collection.count(),
    )
    return collection


def delete_collection(
    client: Optional[chromadb.PersistentClient] = None,
    collection_name: str = COLLECTION_NAME,
) -> None:
    """
    Drop the collection (useful for re-indexing from scratch).

    Parameters
    ----------
    client:
        Optional pre-built ChromaDB client.
    collection_name:
        Name of the collection to delete.
    """
    if client is None:
        client = get_chroma_client()
    try:
        client.delete_collection(collection_name)
        logger.info("Collection '%s' deleted.", collection_name)
    except Exception as exc:
        logger.warning("Could not delete collection '%s': %s", collection_name, exc)


def get_collection_stats(
    collection: Optional[chromadb.Collection] = None,
) -> dict:
    """
    Return basic statistics about the indexed collection.

    Parameters
    ----------
    collection:
        Optional pre-fetched ChromaDB collection.

    Returns
    -------
    Dict with ``total_chunks``, ``unique_blobs``, ``collection_name``.
    """
    if collection is None:
        collection = get_or_create_collection()

    total = collection.count()
    if total == 0:
        return {"total_chunks": 0, "unique_blobs": 0,
                "collection_name": collection.name}

    # Fetch a sample to compute unique blob count
    results = collection.get(limit=total, include=["metadatas"])
    blobs = {m.get("blob_name", "") for m in results["metadatas"]}

    return {
        "total_chunks": total,
        "unique_blobs": len(blobs),
        "collection_name": collection.name,
    }


# ── CLI smoke-test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import tempfile
    from embed import EmbeddedChunk

    # Use a temp dir so smoke-test doesn't pollute the real DB
    with tempfile.TemporaryDirectory() as tmp:
        client = get_chroma_client(persist_dir=tmp)
        col = get_or_create_collection(client=client)

        # Fake chunks with random 4-dim embeddings (real embeddings are 384/1536-dim)
        import random
        fake_chunks = [
            EmbeddedChunk(
                chunk_id=f"test_chunk_{i:04d}",
                blob_name="test/sample.txt",
                text=f"Sample chunk text number {i}.",
                chunk_index=i,
                chunk_total=5,
                embedding=[random.gauss(0, 1) for _ in range(4)],
                metadata={"blob_name": "test/sample.txt", "source_type": "txt",
                          "chunk_index": i, "chunk_total": 5},
                embedding_model="test",
            )
            for i in range(5)
        ]

        col = index_chunks(fake_chunks, collection=col)
        stats = get_collection_stats(col)
        print(f"\nIndex stats: {stats}")
        print("Smoke-test passed.")
