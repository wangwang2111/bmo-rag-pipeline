# Architecture Decisions & Trade-offs

## Overview

The pipeline is designed around three guiding principles:
1. **Modularity** — each stage is independently testable and swappable
2. **Idempotency** — re-running ingest never creates duplicates
3. **Graceful degradation** — Azure-dependent components fall back to free local alternatives

---

## Stage 1: Extraction (`extract.py`)

### PDF routing: digital vs. scanned

**Decision**: Detect scanned PDFs heuristically (avg chars/page < 50) rather than trying OCR on every PDF.

**Why**: PyMuPDF text extraction is ~100× faster than OCR. Running Tesseract on a 100-page digital PDF is pure waste. The 50-char threshold catches genuinely scanned pages while tolerating PDFs with mostly images but some embedded text (e.g., headers/footers).

**Trade-off**: A hybrid PDF (partly digital, partly scanned) will be routed to either path based on the *average* character count. Page-level routing would be more accurate but significantly more complex. For this use case (technical manuals), fully-scanned or fully-digital PDFs are the norm.

### OCR configuration

**Decision**: Run Tesseract at 300 DPI with `lang=eng`.

**Why**: 300 DPI is the minimum recommended for reliable Tesseract OCR. Lower DPI yields unacceptable word-error rates on small fonts.

**Limitation**: Memory usage scales with DPI². For very large scanned PDFs (>50 pages), consider lowering to 200 DPI or processing page-by-page with streaming.

### Markdown parsing

**Decision**: Render Markdown → HTML → strip tags via BeautifulSoup rather than treating `.md` as plain text.

**Why**: Raw Markdown contains `#`, `**`, `[link](url)` syntax that pollutes the chunk text and confuses tokenisers. Stripping to plain text gives cleaner embeddings.

---

## Stage 2: Chunking (`chunk.py`)

### Sentence splitter as default

**Decision**: `SentenceSplitter(chunk_size=512, chunk_overlap=50)` as the default strategy.

**Why**:
- Token-aware: respects LLM context window limits
- Sentence-boundary-aware: does not cut mid-sentence
- Deterministic and fast (no embedding calls)
- 512 tokens ≈ 350–400 words — long enough to capture context, short enough for precise retrieval

**Overlap**: 50-token overlap ensures that information near chunk boundaries is captured by at least one chunk from either side. This is critical for questions that span paragraph breaks.

### Semantic splitter as opt-in

**Decision**: Offer `SemanticSplitter` as an alternative activated by `CHUNKING_STRATEGY=semantic`.

**Why**: For long, unstructured prose (e.g., legal policies), topic-shift-based chunking can outperform fixed-size chunking. However:
- It requires one embedding call per sentence (100–10000× more expensive than `SentenceSplitter`)
- It is non-deterministic (depends on embedding model)
- It produces variable-size chunks that can be very short or very long

**Recommendation**: Use `sentence` for the initial index; switch to `semantic` for a high-quality re-index if retrieval quality is insufficient.

### Minimum chunk size filter

Chunks shorter than 30 characters (page numbers, headers, dividers) are dropped. These micro-chunks add noise to BM25 and vector indexes without contributing retrievable information.

---

## Stage 3: Embeddings (`embed.py`)

### Azure OpenAI `text-embedding-3-small` (primary)

**Decision**: Use Azure OpenAI as the primary embedding provider.

**Why**:
- Managed, scalable, enterprise-grade
- 1536-dim vectors with strong multilingual performance
- Native to the Azure ecosystem (same tenant, same network)

**Limitation**: Requires a paid Azure OpenAI subscription with an embedding deployment. API latency is typically 200–500ms per batch.

### `sentence-transformers/all-MiniLM-L6-v2` (local fallback)

**Decision**: Auto-fall-back to a local sentence-transformer model when Azure credentials are absent.

**Why**: This makes the pipeline runnable offline (e.g., for development, CI/CD, or environments without Azure access). The 384-dim local model is weaker than Azure OpenAI but sufficient for correctness testing.

**Important**: The two models produce vectors of different dimensions (1536 vs 384) and different semantic spaces. **Do not mix** chunks embedded with different models in the same ChromaDB collection. Run `ingest.py --reset` when switching.

### Batching

Embedding calls are batched (default: 32 texts per call) to:
- Reduce round-trip latency (fewer HTTP requests)
- Stay within the Azure OpenAI token-per-request limit (~8192 tokens per call for `text-embedding-3-small`)

---

## Stage 4: Indexing (`index.py`)

### ChromaDB

**Decision**: ChromaDB with persistent on-disk storage.

**Why**:
- Zero-infrastructure: no Docker, no managed service required
- Built-in cosine similarity search
- Native Python API
- Supports metadata filtering in the same query as vector search
- Upsert semantics for idempotent re-indexing

**Trade-off vs. production alternatives**:

| Feature | ChromaDB (this project) | Azure AI Search | Pinecone |
|---|---|---|---|
| Infrastructure | None (embedded) | Managed Azure service | Managed SaaS |
| Scale | ~1M vectors on single node | Billions of vectors | Billions of vectors |
| BM25 built-in | No | Yes | No |
| Metadata filtering | Yes | Yes | Yes |
| Cost | Free | ~$25/month (Basic) | Free tier limited |
| Production readiness | Dev/demo | Enterprise | Enterprise |

For a production BMO deployment, **Azure AI Search** would be the natural replacement: it natively supports hybrid search (semantic + keyword), integrated reranking, and is already within the Azure ecosystem.

### Cosine distance metric

Embeddings are L2-normalised before storage (both models do this). Cosine distance = 1 - dot_product for normalised vectors, making cosine and inner-product distance equivalent. ChromaDB's `hnsw:space=cosine` is set explicitly for clarity.

---

## Stage 5: Hybrid Search (`search.py`)

### Why hybrid (not pure vector)?

Pure vector search misses exact keyword matches (product codes, error codes like "Error 101", proper nouns). Pure BM25 misses semantic paraphrases ("network timeout" vs "connection failure"). Hybrid search captures both.

### Reciprocal Rank Fusion (RRF)

**Decision**: Use RRF to fuse BM25 and vector rankings rather than score normalisation or weighted linear combination.

**Why**:
- **Scale-invariant**: BM25 scores are unbounded; cosine similarities are in [0,1]. Linear weighting requires careful normalisation per query. RRF avoids this entirely.
- **Robust to outliers**: A single very-high BM25 score (e.g., exact phrase match) doesn't dominate at the expense of relevant vector results.
- **Single hyperparameter** (`k=60`): The original paper shows this value works well across diverse retrieval tasks. No dataset-specific tuning needed.
- **Simple and interpretable**: The formula is one line of code.

**Formula**: `RRF(d) = Σᵢ 1/(k + rankᵢ(d))`

**Limitation**: RRF ignores score magnitude. A BM25 match with a score of 0.001 contributes the same as one with 100.0 if they share the same rank position. For very sparse queries this can slightly hurt precision.

### Cross-encoder reranking

**Decision**: Rerank the top-20 RRF candidates with `cross-encoder/ms-marco-MiniLM-L-6-v2`.

**Why**:
- Cross-encoders read query + document jointly (not independently like bi-encoders), giving much better relevance signals
- `ms-marco-MiniLM-L-6-v2` is trained specifically on MS MARCO passage ranking — ideal for this retrieval task
- Applied to only 20 candidates so latency is bounded (~100ms)

**Trade-off**: Adds ~50–200ms to query latency depending on hardware. For sub-50ms SLA requirements, use Cohere Rerank API (GPU-hosted) or skip reranking.

### Semantic captions

**Decision**: Extract the single most relevant sentence from each result chunk using the cross-encoder.

**Why**: Returning an entire 512-token chunk as a "snippet" is noisy. The caption immediately shows the user why the chunk was retrieved.

**Implementation**: We score each sentence independently as a (query, sentence) pair with the cross-encoder. The highest-scoring sentence is the caption. This reuses the already-loaded cross-encoder with no additional model downloads.

---

## Data Flow Summary

```
blob bytes
    ↓ extract.py
DocumentRecord(blob_name, source_type, text, page_count, metadata)
    ↓ chunk.py
ChunkRecord(chunk_id, blob_name, text, chunk_index, chunk_total, metadata)
    ↓ embed.py
EmbeddedChunk(+embedding: List[float], +embedding_model: str)
    ↓ index.py
ChromaDB(id=chunk_id, embedding=..., document=text, metadata=...)
    ↓ search.py
SearchResult(rank, chunk_id, blob_name, text, score, rrf_score,
             bm25_rank, vector_rank, caption, metadata)
```

---

## Scalability Considerations

| Bottleneck | Current approach | At scale |
|---|---|---|
| Blob download | Sequential per blob | Parallel async with `asyncio` + `aiohttp` |
| Embedding generation | Batched API calls | Parallel batches or streaming endpoint |
| BM25 index | In-memory rebuild on startup | Redis / Elasticsearch / Azure AI Search |
| Vector search | ChromaDB local HNSW | Azure AI Search / Pinecone / Weaviate |
| Reranking | Local CPU cross-encoder | Cohere Rerank API or GPU-hosted model |
| Metadata filtering | ChromaDB `where` clause | Partitioned indexes per document type |
