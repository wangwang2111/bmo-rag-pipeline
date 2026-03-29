"""
search.py
=========
Hybrid search combining three retrieval signals:

  1. **BM25** — sparse keyword retrieval over all indexed chunk texts
     (``rank_bm25`` library, Robertson BM25 variant)

  2. **Vector similarity** — dense nearest-neighbour search in ChromaDB
     (cosine distance on L2-normalised embeddings)

  3. **Cross-encoder reranking** — semantic reranking of the fused candidate
     set using ``cross-encoder/ms-marco-MiniLM-L-6-v2``

Score fusion
------------
BM25 and vector scores are on different scales, so we use
**Reciprocal Rank Fusion (RRF)** to combine them purely by rank position:

    RRF(d) = Σ  1 / (k + rank_i(d))
              i

where *k = 60* (standard choice from the original RRF paper).  RRF is
scale-invariant, robust to outliers, and requires no hyper-parameter tuning
beyond *k*.

After RRF fusion, the top ``rerank_candidates`` results are re-scored by the
cross-encoder (which reads both the query and each chunk in full) to produce
the final ranking.

Semantic captions
-----------------
We extract the single most relevant sentence from each result chunk using a
simple sliding-window approach: each sentence is scored independently by the
cross-encoder (query, sentence) and the highest-scoring sentence is returned
as the caption.  This is analogous to Azure Cognitive Search's "semantic
captions" feature.

Public interface
----------------
``SearchResult`` — typed result object
``HybridSearchEngine`` — stateful search engine (loads BM25 index lazily)
``search()`` — convenience function wrapping the engine
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

from embed import _build_embedder, get_query_embedding
from index import get_chroma_client, get_or_create_collection

load_dotenv()

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

TOP_N_RESULTS: int = int(os.getenv("TOP_N_RESULTS", "5"))
RRF_K: int = 60             # Standard RRF constant (Cormack et al. 2009)
BM25_CANDIDATES: int = 50   # How many BM25 hits to pull before fusion
VECTOR_CANDIDATES: int = 50 # How many vector hits to pull before fusion
RERANK_CANDIDATES: int = 20 # How many fused candidates to pass to cross-encoder


# ── Public data contract ──────────────────────────────────────────────────────

@dataclass
class SearchResult:
    """A single ranked result returned by the hybrid search engine."""

    rank: int
    """1-based rank in the final result set."""

    chunk_id: str
    """Unique identifier of the retrieved chunk."""

    blob_name: str
    """Source document blob path."""

    text: str
    """Full chunk text."""

    score: float
    """Final reranker score (higher = more relevant)."""

    rrf_score: float
    """RRF fusion score before reranking."""

    bm25_rank: Optional[int]
    """Rank in BM25 results (None if not retrieved by BM25)."""

    vector_rank: Optional[int]
    """Rank in vector results (None if not retrieved by vector search)."""

    caption: str = ""
    """Most relevant sentence extracted from the chunk for the given query."""

    metadata: dict = field(default_factory=dict)
    """Full metadata inherited from the indexed chunk."""


# ── BM25 index ────────────────────────────────────────────────────────────────

class BM25Index:
    """
    In-memory BM25 index built lazily from ChromaDB stored documents.

    We load all chunk texts from ChromaDB once, tokenise them, and build
    a BM25Okapi index.  The index is rebuilt if ``refresh()`` is called.

    Note: For very large collections (>1M chunks) this in-memory approach
    should be replaced with a dedicated keyword search service (Elasticsearch,
    Azure Cognitive Search, etc.).
    """

    def __init__(self) -> None:
        self._bm25 = None
        self._chunk_ids: list[str] = []
        self._chunk_texts: list[str] = []
        self._chunk_metadatas: list[dict] = []
        self._id_to_index: dict[str, int] = {}

    def build(self, collection) -> None:
        """
        Load all documents from the ChromaDB collection and build the BM25 index.

        Parameters
        ----------
        collection:
            ChromaDB collection instance.
        """
        try:
            from rank_bm25 import BM25Okapi
        except ImportError as exc:
            raise ImportError("pip install rank-bm25") from exc

        total = collection.count()
        if total == 0:
            raise ValueError(
                "ChromaDB collection is empty. Run ingest.py before searching."
            )

        logger.info("Building BM25 index over %d chunks …", total)
        results = collection.get(
            limit=total,
            include=["documents", "metadatas"],
        )
        self._chunk_ids = results["ids"]
        self._chunk_texts = results["documents"]
        self._chunk_metadatas = results["metadatas"]
        self._id_to_index = {cid: i for i, cid in enumerate(self._chunk_ids)}

        # Tokenise: lowercase, split on non-word characters
        tokenised = [self._tokenise(t) for t in self._chunk_texts]
        self._bm25 = BM25Okapi(tokenised)
        logger.info("BM25 index built (%d documents).", total)

    def refresh(self, collection) -> None:
        """Rebuild the BM25 index (call after re-indexing new documents)."""
        self.build(collection)

    @staticmethod
    def _tokenise(text: str) -> list[str]:
        """Simple whitespace + punctuation tokeniser (lowercase)."""
        return re.split(r"\W+", text.lower())

    def query(self, query_text: str, top_k: int = BM25_CANDIDATES) -> list[tuple[str, float, int]]:
        """
        Run a BM25 query and return ranked (chunk_id, score, rank) tuples.

        Parameters
        ----------
        query_text:
            The search query string.
        top_k:
            Number of top results to return.

        Returns
        -------
        List of ``(chunk_id, bm25_score, rank)`` tuples sorted by score desc.
        """
        if self._bm25 is None:
            raise RuntimeError("BM25 index not built. Call build() first.")

        tokens = self._tokenise(query_text)
        scores = self._bm25.get_scores(tokens)

        # Sort by score descending, take top_k
        ranked = sorted(
            enumerate(scores), key=lambda x: x[1], reverse=True
        )[:top_k]

        results: list[tuple[str, float, int]] = []
        for rank_pos, (idx, score) in enumerate(ranked, start=1):
            if score > 0:  # Only include docs with non-zero BM25 score
                results.append((self._chunk_ids[idx], float(score), rank_pos))

        return results

    def get_text_by_id(self, chunk_id: str) -> Optional[str]:
        """Look up stored chunk text by its ID."""
        idx = self._id_to_index.get(chunk_id)
        return self._chunk_texts[idx] if idx is not None else None

    def get_metadata_by_id(self, chunk_id: str) -> Optional[dict]:
        """Look up stored chunk metadata by its ID."""
        idx = self._id_to_index.get(chunk_id)
        return self._chunk_metadatas[idx] if idx is not None else None


# ── Reciprocal Rank Fusion ────────────────────────────────────────────────────

def reciprocal_rank_fusion(
    bm25_results: list[tuple[str, float, int]],
    vector_results: list[tuple[str, float, int]],
    k: int = RRF_K,
) -> list[tuple[str, float, Optional[int], Optional[int]]]:
    """
    Fuse BM25 and vector rankings using Reciprocal Rank Fusion.

    RRF is scale-invariant — it ignores the raw BM25 / cosine scores and
    combines purely by rank position.  This avoids the need to normalise
    scores from two very different distributions.

    Parameters
    ----------
    bm25_results:
        List of ``(chunk_id, score, rank)`` from BM25.
    vector_results:
        List of ``(chunk_id, score, rank)`` from vector search.
    k:
        RRF constant (default 60, from original paper).

    Returns
    -------
    List of ``(chunk_id, rrf_score, bm25_rank, vector_rank)`` sorted by
    ``rrf_score`` descending.
    """
    rrf_scores: dict[str, float] = {}
    bm25_ranks: dict[str, int] = {}
    vector_ranks: dict[str, int] = {}

    for chunk_id, _score, rank in bm25_results:
        rrf_scores[chunk_id] = rrf_scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
        bm25_ranks[chunk_id] = rank

    for chunk_id, _score, rank in vector_results:
        rrf_scores[chunk_id] = rrf_scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
        vector_ranks[chunk_id] = rank

    fused = [
        (cid, score, bm25_ranks.get(cid), vector_ranks.get(cid))
        for cid, score in rrf_scores.items()
    ]
    fused.sort(key=lambda x: x[1], reverse=True)
    return fused


# ── Cross-encoder reranker ────────────────────────────────────────────────────

class CrossEncoderReranker:
    """
    Semantic reranker using ``cross-encoder/ms-marco-MiniLM-L-6-v2``.

    A cross-encoder reads both the query and the document simultaneously
    (as a single sequence) which gives much better relevance scores than
    bi-encoders (which produce independent embeddings).  The trade-off is
    latency: it cannot be pre-indexed, so it is only applied to a small
    candidate set after initial retrieval.
    """

    MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    def __init__(self) -> None:
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:
            raise ImportError("pip install sentence-transformers") from exc

        logger.info("Loading cross-encoder '%s' …", self.MODEL_NAME)
        self._model = CrossEncoder(self.MODEL_NAME)
        logger.info("CrossEncoderReranker ready.")

    def rerank(
        self,
        query: str,
        candidates: list[tuple[str, str]],  # (chunk_id, text)
    ) -> list[tuple[str, float]]:
        """
        Score each (query, document) pair and return sorted ``(chunk_id, score)``.

        Parameters
        ----------
        query:
            Search query.
        candidates:
            List of ``(chunk_id, chunk_text)`` pairs.

        Returns
        -------
        ``(chunk_id, relevance_score)`` list sorted by score descending.
        """
        if not candidates:
            return []

        pairs = [(query, text) for _, text in candidates]
        scores = self._model.predict(pairs)

        ranked = sorted(
            zip([cid for cid, _ in candidates], scores),
            key=lambda x: x[1],
            reverse=True,
        )
        return [(cid, float(score)) for cid, score in ranked]

    def score_sentence(self, query: str, sentence: str) -> float:
        """Score a single (query, sentence) pair — used for caption extraction."""
        scores = self._model.predict([(query, sentence)])
        return float(scores[0])


# ── Semantic caption extraction ───────────────────────────────────────────────

def extract_caption(
    query: str,
    chunk_text: str,
    reranker: CrossEncoderReranker,
    max_caption_chars: int = 300,
) -> str:
    """
    Extract the most relevant sentence from a chunk as a semantic caption.

    We split the chunk into sentences and score each (query, sentence) pair
    with the cross-encoder.  The highest-scoring sentence becomes the caption,
    truncated to ``max_caption_chars`` if needed.

    Parameters
    ----------
    query:
        The search query.
    chunk_text:
        The full chunk text.
    reranker:
        An initialised ``CrossEncoderReranker``.
    max_caption_chars:
        Maximum character length for the returned caption.

    Returns
    -------
    The most relevant sentence (string).
    """
    # Simple sentence splitter: split on ". ", "? ", "! " followed by uppercase
    sentences = re.split(r"(?<=[.?!])\s+", chunk_text.strip())
    sentences = [s.strip() for s in sentences if len(s.strip()) > 10]

    if not sentences:
        return chunk_text[:max_caption_chars]

    if len(sentences) == 1:
        return sentences[0][:max_caption_chars]

    # Score each sentence — batch predict for efficiency
    pairs = [(query, s) for s in sentences]
    scores = reranker._model.predict(pairs)
    best_idx = int(scores.argmax())
    caption = sentences[best_idx]
    return caption[:max_caption_chars]


# ── Main search engine ────────────────────────────────────────────────────────

class HybridSearchEngine:
    """
    Stateful search engine that combines BM25 + vector search + reranking.

    Initialisation lazily builds the BM25 index the first time ``search()``
    is called so startup is fast.

    Parameters
    ----------
    top_n:
        Number of final results to return (default from ``TOP_N_RESULTS`` env var).
    rerank_candidates:
        How many fused candidates to pass to the cross-encoder (default 20).
    """

    def __init__(
        self,
        top_n: int = TOP_N_RESULTS,
        rerank_candidates: int = RERANK_CANDIDATES,
    ) -> None:
        self.top_n = top_n
        self.rerank_candidates = rerank_candidates

        self._collection = None
        self._bm25_index: Optional[BM25Index] = None
        self._embedder = None
        self._reranker: Optional[CrossEncoderReranker] = None

    def _ensure_ready(self) -> None:
        """Lazily initialise all components on first use."""
        if self._collection is None:
            client = get_chroma_client()
            self._collection = get_or_create_collection(client=client)

        if self._bm25_index is None:
            self._bm25_index = BM25Index()
            self._bm25_index.build(self._collection)

        if self._embedder is None:
            self._embedder = _build_embedder()

        if self._reranker is None:
            self._reranker = CrossEncoderReranker()

    def refresh_bm25(self) -> None:
        """Rebuild the BM25 index (call after adding new documents)."""
        if self._bm25_index and self._collection:
            self._bm25_index.refresh(self._collection)
        else:
            self._ensure_ready()

    def search(
        self,
        query: str,
        top_n: Optional[int] = None,
        filter_metadata: Optional[dict] = None,
    ) -> list[SearchResult]:
        """
        Run the full hybrid search pipeline for a query.

        Pipeline:
        1. Embed query → dense vector
        2. BM25 keyword search over all chunks
        3. ChromaDB vector similarity search
        4. RRF fusion of BM25 + vector rankings
        5. Cross-encoder reranking of top candidates
        6. Semantic caption extraction for each result

        Parameters
        ----------
        query:
            Natural language search query.
        top_n:
            Number of results to return (overrides instance default).
        filter_metadata:
            Optional ChromaDB ``where`` filter, e.g.
            ``{"source_type": "pdf_digital"}``.

        Returns
        -------
        Ranked list of ``SearchResult`` objects.
        """
        n = top_n or self.top_n
        self._ensure_ready()

        logger.info("Hybrid search: query='%s', top_n=%d", query, n)
        t_total = time.perf_counter()

        # ── Step 1: Query embedding ───────────────────────────────────────────
        t0 = time.perf_counter()
        query_vec = get_query_embedding(query, embedder=self._embedder)
        t_embed = (time.perf_counter() - t0) * 1000

        # ── Step 2: BM25 keyword search ───────────────────────────────────────
        t0 = time.perf_counter()
        bm25_hits = self._bm25_index.query(query, top_k=BM25_CANDIDATES)
        t_bm25 = (time.perf_counter() - t0) * 1000
        logger.debug("BM25 returned %d hits.", len(bm25_hits))

        # ── Step 3: Vector similarity search ─────────────────────────────────
        chroma_kwargs: dict = {
            "query_embeddings": [query_vec],
            "n_results": min(VECTOR_CANDIDATES, self._collection.count()),
            "include": ["documents", "metadatas", "distances"],
        }
        if filter_metadata:
            chroma_kwargs["where"] = filter_metadata

        t0 = time.perf_counter()
        vec_response = self._collection.query(**chroma_kwargs)
        t_vector = (time.perf_counter() - t0) * 1000

        # Convert ChromaDB distances (cosine distance) to ranks
        vec_ids = vec_response["ids"][0]
        vec_distances = vec_response["distances"][0]
        vec_docs = vec_response["documents"][0]
        vec_metas = vec_response["metadatas"][0]

        # Build a lookup: chunk_id → (text, metadata)
        id_to_text: dict[str, str] = {}
        id_to_meta: dict[str, dict] = {}
        for cid, doc, meta in zip(vec_ids, vec_docs, vec_metas):
            id_to_text[cid] = doc
            id_to_meta[cid] = meta

        # Also add BM25 results to the lookup
        # vector results  →  text came back from ChromaDB query  →  already in id_to_text
        #   BM25-only hits  →  text not returned by ChromaDB       →  must fetch from BM25 list
        for cid, _, _ in bm25_hits:
            if cid not in id_to_text:
                text = self._bm25_index.get_text_by_id(cid)
                meta = self._bm25_index.get_metadata_by_id(cid)
                if text:
                    id_to_text[cid] = text
                if meta:
                    id_to_meta[cid] = meta

        # Build ranked vector results list: (chunk_id, cosine_similarity, rank)
        vector_hits: list[tuple[str, float, int]] = [
            (cid, 1.0 - dist, rank)
            for rank, (cid, dist) in enumerate(zip(vec_ids, vec_distances), start=1)
        ]
        logger.debug("Vector search returned %d hits.", len(vector_hits))

        # ── Step 4: RRF fusion ────────────────────────────────────────────────
        t0 = time.perf_counter()
        fused = reciprocal_rank_fusion(bm25_hits, vector_hits)
        t_rrf = (time.perf_counter() - t0) * 1000
        logger.debug("RRF fusion produced %d unique candidates.", len(fused))

        # Take top candidates for reranking
        top_candidates = fused[: self.rerank_candidates]

        # ── Step 5: Cross-encoder reranking ───────────────────────────────────
        rerank_input = [
            (cid, id_to_text[cid])
            for cid, _, _, _ in top_candidates
            if cid in id_to_text
        ]
        t0 = time.perf_counter()
        reranked = self._reranker.rerank(query, rerank_input)
        t_rerank = (time.perf_counter() - t0) * 1000
        logger.debug("Reranker scored %d candidates.", len(reranked))

        # Build a score lookup for RRF scores
        rrf_lookup = {cid: rrf for cid, rrf, _, _ in fused}
        bm25_rank_lookup = {cid: br for cid, _, br, _ in fused}
        vec_rank_lookup = {cid: vr for cid, _, _, vr in fused}

        # ── Step 6: Assemble results with captions ────────────────────────────
        t0 = time.perf_counter()
        results: list[SearchResult] = []
        for rank_pos, (chunk_id, rerank_score) in enumerate(reranked[:n], start=1):
            text = id_to_text.get(chunk_id, "")
            meta = id_to_meta.get(chunk_id, {})
            caption = extract_caption(query, text, self._reranker)

            results.append(
                SearchResult(
                    rank=rank_pos,
                    chunk_id=chunk_id,
                    blob_name=meta.get("blob_name", "unknown"),
                    text=text,
                    score=rerank_score,
                    rrf_score=rrf_lookup.get(chunk_id, 0.0),
                    bm25_rank=bm25_rank_lookup.get(chunk_id),
                    vector_rank=vec_rank_lookup.get(chunk_id),
                    caption=caption,
                    metadata=meta,
                )
            )
        t_captions = (time.perf_counter() - t0) * 1000

        t_total_ms = (time.perf_counter() - t_total) * 1000
        logger.info(
            "Latency (ms) — embed: %.1f | bm25: %.1f | vector: %.1f | "
            "rrf: %.1f | rerank: %.1f | captions: %.1f | total: %.1f",
            t_embed, t_bm25, t_vector, t_rrf, t_rerank, t_captions, t_total_ms,
        )

        return results


# ── Module-level convenience function ────────────────────────────────────────

_default_engine: Optional[HybridSearchEngine] = None


def search(
    query: str,
    top_n: int = TOP_N_RESULTS,
    filter_metadata: Optional[dict] = None,
) -> list[SearchResult]:
    """
    Module-level convenience wrapper around ``HybridSearchEngine``.

    Uses a lazily initialised singleton engine so the BM25 index and model
    weights are loaded only once per process.

    Parameters
    ----------
    query:
        Natural language search query.
    top_n:
        Number of results.
    filter_metadata:
        Optional ChromaDB ``where`` filter dict.

    Returns
    -------
    Ranked list of ``SearchResult`` objects.
    """
    global _default_engine
    if _default_engine is None:
        _default_engine = HybridSearchEngine(top_n=top_n)
    return _default_engine.search(query, top_n=top_n, filter_metadata=filter_metadata)


def format_results(results: list[SearchResult], show_full_text: bool = False) -> str:
    """
    Pretty-print search results to a human-readable string.

    Parameters
    ----------
    results:
        Ranked search results.
    show_full_text:
        If ``True``, include full chunk text; otherwise show only the caption.

    Returns
    -------
    Formatted string.
    """
    lines: list[str] = []
    for r in results:
        lines.append(f"\n{'─' * 60}")
        lines.append(f"Rank #{r.rank}  |  Score: {r.score:.4f}  |  RRF: {r.rrf_score:.4f}")
        lines.append(f"Source : {r.blob_name}")
        lines.append(f"Chunk  : {r.chunk_id}")
        lines.append(f"BM25 rank: {r.bm25_rank}  |  Vector rank: {r.vector_rank}")
        lines.append(f"Caption: {r.caption}")
        if show_full_text:
            lines.append(f"\nFull text:\n{r.text}")
    return "\n".join(lines)


# ── CLI smoke-test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run a hybrid search query.")
    parser.add_argument("query", help="Search query string.")
    parser.add_argument("--top-n", type=int, default=TOP_N_RESULTS)
    parser.add_argument("--full-text", action="store_true")
    parser.add_argument("--source-type", help="Filter by source_type metadata field.")
    args = parser.parse_args()

    metadata_filter = None
    if args.source_type:
        metadata_filter = {"source_type": args.source_type}

    results = search(args.query, top_n=args.top_n, filter_metadata=metadata_filter)
    print(format_results(results, show_full_text=args.full_text))
