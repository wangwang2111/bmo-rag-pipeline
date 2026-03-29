"""
embed.py
========
Generates dense vector embeddings for ``ChunkRecord`` text using:

  1. **Azure OpenAI** ``text-embedding-3-small`` (primary, 1536-dim)
  2. **sentence-transformers** ``all-MiniLM-L6-v2`` (local fallback, 384-dim)

The module auto-selects the provider: if Azure OpenAI credentials are present
in the environment it uses the API; otherwise it falls back to the local model
with a warning.

Design decisions
----------------
* Batching: Azure OpenAI has a max-tokens-per-request limit.  We batch chunks
  into groups of ``EMBEDDING_BATCH_SIZE`` (default 32) to stay within limits
  and reduce latency via fewer round-trips.
* Retry: transient API errors are retried up to ``MAX_RETRIES`` times with
  exponential back-off.
* The returned ``EmbeddedChunk`` carries both the original ``ChunkRecord``
  fields and the embedding vector, keeping downstream modules decoupled from
  the embedding provider.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from dotenv import load_dotenv

from chunk import ChunkRecord

load_dotenv()

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

EMBEDDING_BATCH_SIZE: int = int(os.getenv("EMBEDDING_BATCH_SIZE", "32"))
MAX_RETRIES: int = 3
RETRY_BASE_DELAY: float = 1.0  # seconds; doubles on each retry


# ── Public data contract ──────────────────────────────────────────────────────

@dataclass
class EmbeddedChunk:
    """A ``ChunkRecord`` augmented with its dense embedding vector."""

    chunk_id: str
    blob_name: str
    text: str
    chunk_index: int
    chunk_total: int
    embedding: list[float]
    metadata: dict = field(default_factory=dict)
    embedding_model: str = ""
    """Name of the model that produced the embedding, for audit/metadata."""

    @classmethod
    def from_chunk(
        cls,
        chunk: ChunkRecord,
        embedding: list[float],
        model_name: str,
    ) -> "EmbeddedChunk":
        """Construct from an existing ``ChunkRecord`` + embedding vector."""
        return cls(
            chunk_id=chunk.chunk_id,
            blob_name=chunk.blob_name,
            text=chunk.text,
            chunk_index=chunk.chunk_index,
            chunk_total=chunk.chunk_total,
            embedding=embedding,
            metadata={**chunk.metadata, "embedding_model": model_name},
            embedding_model=model_name,
        )


# ── Provider: Azure OpenAI ────────────────────────────────────────────────────

class AzureOpenAIEmbedder:
    """
    Wraps the Azure OpenAI embeddings endpoint.

    Authentication is via ``AZURE_OPENAI_API_KEY`` + ``AZURE_OPENAI_ENDPOINT``.
    The deployment name defaults to ``text-embedding-3-small``.
    """

    def __init__(self) -> None:
        try:
            from openai import AzureOpenAI
        except ImportError as exc:
            raise ImportError("pip install openai") from exc

        api_key = os.environ["AZURE_OPENAI_API_KEY"]
        endpoint = os.environ["AZURE_OPENAI_ENDPOINT"]
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "text-embedding-3-small")
        api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")

        self._client = AzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version=api_version,
        )
        self._deployment = deployment
        self.model_name = f"azure-openai/{deployment}"
        logger.info("AzureOpenAIEmbedder ready (deployment=%s).", deployment)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Embed a batch of texts and return their vectors.

        Retries up to ``MAX_RETRIES`` times on transient failures.
        """
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self._client.embeddings.create(
                    input=texts,
                    model=self._deployment,
                )
                # Preserve ordering — Azure returns items in input order
                vectors = [item.embedding for item in sorted(
                    response.data, key=lambda x: x.index
                )]
                return vectors
            except Exception as exc:
                if attempt == MAX_RETRIES:
                    raise
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "Embedding attempt %d/%d failed (%s). Retrying in %.1fs …",
                    attempt, MAX_RETRIES, exc, delay,
                )
                time.sleep(delay)
        # Unreachable, but satisfies type checkers
        raise RuntimeError("Embedding failed after retries.")


# ── Provider: sentence-transformers (local fallback) ─────────────────────────

class LocalEmbedder:
    """
    Generates embeddings locally using ``sentence-transformers``.

    Model: ``sentence-transformers/all-MiniLM-L6-v2`` (384-dim, ~90 MB).
    This requires no API key and works fully offline.
    """

    MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

    def __init__(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ImportError("pip install sentence-transformers") from exc

        logger.info("Loading local embedding model '%s' …", self.MODEL_NAME)
        self._model = SentenceTransformer(self.MODEL_NAME)
        self.model_name = f"local/{self.MODEL_NAME}"
        logger.info("LocalEmbedder ready.")

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts and return their vectors as Python lists."""
        # encode() returns a numpy ndarray of shape (n_texts, dim)
        vectors: np.ndarray = self._model.encode(
            texts,
            batch_size=EMBEDDING_BATCH_SIZE,
            show_progress_bar=False,
            normalize_embeddings=True,  # L2-normalise for cosine similarity
        )
        return vectors.tolist()


# ── Provider selection ────────────────────────────────────────────────────────

def _build_embedder() -> AzureOpenAIEmbedder | LocalEmbedder:
    """
    Return the best available embedding provider.

    Tries Azure OpenAI first; falls back to local sentence-transformers if
    credentials are absent or the Azure SDK import fails.
    """
    if os.getenv("AZURE_OPENAI_API_KEY") and os.getenv("AZURE_OPENAI_ENDPOINT"):
        try:
            return AzureOpenAIEmbedder()
        except Exception as exc:
            logger.warning("Azure OpenAI embedder failed to initialise (%s); "
                           "falling back to local model.", exc)

    logger.info("Using local sentence-transformers fallback for embeddings.")
    return LocalEmbedder()


# ── Public API ────────────────────────────────────────────────────────────────

def embed_chunks(
    chunks: list[ChunkRecord],
    embedder: Optional[AzureOpenAIEmbedder | LocalEmbedder] = None,
) -> list[EmbeddedChunk]:
    """
    Generate embeddings for a list of ``ChunkRecord`` objects.

    Parameters
    ----------
    chunks:
        Chunks to embed.
    embedder:
        Optional pre-constructed embedder.  If ``None``, one is built via
        :func:`_build_embedder`.

    Returns
    -------
    List of ``EmbeddedChunk`` objects in the same order as ``chunks``.
    """
    if not chunks:
        logger.warning("embed_chunks called with empty chunk list.")
        return []

    if embedder is None:
        embedder = _build_embedder()

    embedded: list[EmbeddedChunk] = []
    total = len(chunks)

    # Process in batches to respect API limits and show progress
    for batch_start in range(0, total, EMBEDDING_BATCH_SIZE):
        batch = chunks[batch_start: batch_start + EMBEDDING_BATCH_SIZE]
        texts = [c.text for c in batch]

        logger.debug(
            "Embedding batch %d–%d / %d …",
            batch_start + 1,
            min(batch_start + EMBEDDING_BATCH_SIZE, total),
            total,
        )

        vectors = embedder.embed_batch(texts)

        for chunk, vector in zip(batch, vectors):
            embedded.append(
                EmbeddedChunk.from_chunk(chunk, vector, embedder.model_name)
            )

    logger.info(
        "Generated %d embeddings (dim=%d, model=%s).",
        len(embedded),
        len(embedded[0].embedding) if embedded else 0,
        embedder.model_name,
    )
    return embedded


def get_query_embedding(
    query: str,
    embedder: Optional[AzureOpenAIEmbedder | LocalEmbedder] = None,
) -> list[float]:
    """
    Embed a single query string for similarity search.

    Parameters
    ----------
    query:
        The search query.
    embedder:
        Optional pre-constructed embedder.

    Returns
    -------
    Embedding vector as a Python list of floats.
    """
    if embedder is None:
        embedder = _build_embedder()

    vectors = embedder.embed_batch([query])
    return vectors[0]


# ── CLI smoke-test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from chunk import ChunkRecord

    sample_chunks = [
        ChunkRecord(
            chunk_id="test_chunk_0000",
            blob_name="test/sample.txt",
            text="Error 101 indicates a network timeout on the device.",
            chunk_index=0,
            chunk_total=2,
            metadata={"source_type": "txt", "blob_name": "test/sample.txt"},
        ),
        ChunkRecord(
            chunk_id="test_chunk_0001",
            blob_name="test/sample.txt",
            text="To resolve error 101, restart the device and check firewall settings.",
            chunk_index=1,
            chunk_total=2,
            metadata={"source_type": "txt", "blob_name": "test/sample.txt"},
        ),
    ]

    results = embed_chunks(sample_chunks)
    for ec in results:
        vec = ec.embedding
        print(f"\nChunk : {ec.chunk_id}")
        print(f"Model : {ec.embedding_model}")
        print(f"Dim   : {len(vec)}")
        print(f"Norm  : {sum(v**2 for v in vec)**0.5:.4f}")
        print(f"Text  : {ec.text[:80]}")
