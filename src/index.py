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


# ── Azure AI Search backend ───────────────────────────────────────────────────

AZURE_SEARCH_INDEX_NAME: str = os.getenv("AZURE_SEARCH_INDEX_NAME", "bmo-rag-chunks")
AZURE_SEARCH_VECTOR_DIMS: int = int(os.getenv("AZURE_SEARCH_VECTOR_DIMS", "1536"))
VECTOR_BACKEND: str = os.getenv("VECTOR_BACKEND", "chroma").lower()


class AzureAISearchIndexer:
    """
    Indexes ``EmbeddedChunk`` objects into Azure AI Search.

    The index schema is created (or updated) automatically on first use via
    ``create_or_update_index`` — fully idempotent.  Uploads use
    ``merge_or_upload_documents`` so re-running ingest never creates duplicates.

    Azure AI Search natively provides BM25 keyword search, vector search, RRF
    fusion, semantic reranking, and extractive captions — all from a single
    search API call — replacing the manual pipeline in ``search.py``.

    Required env vars
    -----------------
    AZURE_SEARCH_ENDPOINT    e.g. https://<service>.search.windows.net
    AZURE_SEARCH_KEY         Admin API key
    AZURE_SEARCH_INDEX_NAME  Index name (default: bmo-rag-chunks)
    AZURE_SEARCH_VECTOR_DIMS Embedding dimensions: 1536 (Azure OpenAI) or
                             384 (all-MiniLM-L6-v2 local fallback)
    """

    _SEMANTIC_CONFIG = "bmo-semantic"
    _VECTOR_PROFILE = "hnsw-profile"

    def __init__(self) -> None:
        try:
            from azure.core.credentials import AzureKeyCredential
            from azure.search.documents import SearchClient
            from azure.search.documents.indexes import SearchIndexClient
        except ImportError as exc:
            raise ImportError(
                "pip install azure-search-documents>=11.4.0"
            ) from exc

        from azure.core.credentials import AzureKeyCredential
        from azure.search.documents import SearchClient
        from azure.search.documents.indexes import SearchIndexClient

        endpoint = os.environ["AZURE_SEARCH_ENDPOINT"]
        key = os.environ["AZURE_SEARCH_KEY"]
        credential = AzureKeyCredential(key)

        self.index_name = AZURE_SEARCH_INDEX_NAME
        self.vector_dims = AZURE_SEARCH_VECTOR_DIMS
        self._index_client = SearchIndexClient(endpoint, credential)
        self._search_client = SearchClient(endpoint, self.index_name, credential)
        self._index_ready = False
        logger.debug("AzureAISearchIndexer ready (index=%s).", self.index_name)

    def _ensure_index(self) -> None:
        """Create or update the search index schema (idempotent)."""
        if self._index_ready:
            return

        from azure.search.documents.indexes.models import (
            HnswAlgorithmConfiguration,
            SearchField,
            SearchFieldDataType,
            SearchIndex,
            SemanticConfiguration,
            SemanticField,
            SemanticPrioritizedFields,
            SemanticSearch,
            SimpleField,
            SearchableField,
            VectorSearch,
            VectorSearchProfile,
        )

        fields = [
            SimpleField(name="chunk_id", type=SearchFieldDataType.String,
                        key=True, filterable=True),
            SearchableField(name="text", type=SearchFieldDataType.String,
                            analyzer_name="en.lucene"),
            SimpleField(name="blob_name", type=SearchFieldDataType.String,
                        filterable=True, facetable=True),
            SimpleField(name="source_type", type=SearchFieldDataType.String,
                        filterable=True, facetable=True),
            SimpleField(name="chunk_index", type=SearchFieldDataType.Int32,
                        filterable=True),
            SimpleField(name="chunk_total", type=SearchFieldDataType.Int32),
            SimpleField(name="page_count", type=SearchFieldDataType.Int32),
            SimpleField(name="embedding_model", type=SearchFieldDataType.String,
                        filterable=True),
            SearchField(
                name="embedding",
                type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                searchable=True,
                vector_search_dimensions=self.vector_dims,
                vector_search_profile_name=self._VECTOR_PROFILE,
            ),
        ]

        vector_search = VectorSearch(
            algorithms=[HnswAlgorithmConfiguration(name="hnsw-algo")],
            profiles=[VectorSearchProfile(
                name=self._VECTOR_PROFILE,
                algorithm_configuration_name="hnsw-algo",
            )],
        )

        semantic_search = SemanticSearch(
            configurations=[
                SemanticConfiguration(
                    name=self._SEMANTIC_CONFIG,
                    prioritized_fields=SemanticPrioritizedFields(
                        content_fields=[SemanticField(field_name="text")]
                    ),
                )
            ]
        )

        index = SearchIndex(
            name=self.index_name,
            fields=fields,
            vector_search=vector_search,
            semantic_search=semantic_search,
        )
        self._index_client.create_or_update_index(index)
        self._index_ready = True
        logger.info("Azure AI Search index '%s' ready.", self.index_name)

    def index_chunks(
        self,
        chunks: list[EmbeddedChunk],
        batch_size: int = INDEX_BATCH_SIZE,
    ) -> dict:
        """
        Upload ``EmbeddedChunk`` objects using merge-or-upload semantics.

        Returns a stats dict with ``total_chunks``, ``unique_blobs``, and
        ``index_name``.
        """
        if not chunks:
            logger.warning("index_chunks called with empty list.")
            return {"total_chunks": 0, "unique_blobs": 0,
                    "index_name": self.index_name}

        self._ensure_index()
        total = len(chunks)

        for batch_start in range(0, total, batch_size):
            batch = chunks[batch_start: batch_start + batch_size]
            documents = [
                {
                    "chunk_id": c.chunk_id,
                    "text": c.text,
                    "blob_name": c.metadata.get("blob_name", ""),
                    "source_type": c.metadata.get("source_type", ""),
                    "chunk_index": int(c.metadata.get("chunk_index", 0)),
                    "chunk_total": int(c.metadata.get("chunk_total", 0)),
                    "page_count": int(c.metadata.get("page_count", 0)),
                    "embedding_model": c.embedding_model,
                    "embedding": c.embedding,
                }
                for c in batch
            ]
            self._search_client.merge_or_upload_documents(documents)
            logger.debug(
                "Uploaded batch %d–%d / %d to Azure AI Search.",
                batch_start + 1, batch_start + len(batch), total,
            )

        unique_blobs = {c.metadata.get("blob_name", "") for c in chunks}
        logger.info(
            "Indexed %d chunks into Azure AI Search index '%s'.",
            total, self.index_name,
        )
        return {
            "total_chunks": total,
            "unique_blobs": len(unique_blobs),
            "index_name": self.index_name,
        }

    def delete_index(self) -> None:
        """Delete the Azure AI Search index."""
        try:
            self._index_client.delete_index(self.index_name)
            self._index_ready = False
            logger.info("Azure AI Search index '%s' deleted.", self.index_name)
        except Exception as exc:
            logger.warning("Could not delete index '%s': %s", self.index_name, exc)

    def get_stats(self) -> dict:
        """Return document count and index name from the Azure AI Search service."""
        try:
            stats = self._index_client.get_index_statistics(self.index_name)
            return {
                "total_chunks": stats.document_count,
                "index_name": self.index_name,
            }
        except Exception as exc:
            logger.warning("Could not fetch index stats: %s", exc)
            return {"index_name": self.index_name}


class _ChromaDBIndexer:
    """
    Thin wrapper around the ChromaDB module functions conforming to the same
    interface as ``AzureAISearchIndexer``.

    Not intended for direct use — instantiate via ``get_indexer()``.
    """

    def __init__(self) -> None:
        self._client = get_chroma_client()
        self._collection = get_or_create_collection(client=self._client)

    def index_chunks(
        self,
        chunks: list[EmbeddedChunk],
        batch_size: int = INDEX_BATCH_SIZE,
    ) -> dict:
        """Upsert chunks into ChromaDB and return collection stats."""
        # Calls the module-level index_chunks function (not this method).
        index_chunks(chunks, collection=self._collection, batch_size=batch_size)
        return get_collection_stats(self._collection)

    def delete_index(self) -> None:
        """Drop the ChromaDB collection."""
        delete_collection(client=self._client)

    def get_stats(self) -> dict:
        """Return collection stats."""
        return get_collection_stats(self._collection)


def get_indexer() -> "_ChromaDBIndexer | AzureAISearchIndexer":
    """
    Return the configured vector-store indexer.

    Controlled by the ``VECTOR_BACKEND`` environment variable:

    ``chroma`` (default)
        Local ChromaDB — zero infrastructure, ideal for development and demos.
    ``azure_ai_search``
        Azure AI Search — managed, enterprise-grade, native hybrid search.
        Requires ``AZURE_SEARCH_ENDPOINT``, ``AZURE_SEARCH_KEY``, and
        optionally ``AZURE_SEARCH_INDEX_NAME`` and ``AZURE_SEARCH_VECTOR_DIMS``.
    """
    if VECTOR_BACKEND == "azure_ai_search":
        logger.info("VECTOR_BACKEND=azure_ai_search — using AzureAISearchIndexer.")
        return AzureAISearchIndexer()
    logger.info("VECTOR_BACKEND=chroma — using ChromaDB.")
    return _ChromaDBIndexer()


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
