# Implementation Plan: BMO Azure ETL and RAG Pipeline

## 1. Problem Statement

The goal is to build a retrieval-augmented search system over a mixed-format document corpus
(PDFs, Markdown, TXT) stored in Azure Blob Storage. The system must handle scanned documents,
support hybrid retrieval, and return ranked results with source attribution and semantic captions.

The core challenge is **retrieval quality**, not plumbing. A pipeline that ingests documents
but retrieves poorly is useless. Every architectural decision was made in service of that constraint.

## 2. Pipeline Architecture Overview

The problem was decomposed into six discrete stages, each with a clearly defined input type,
output type, and set of failure modes. Defining typed dataclasses at each stage boundary before
writing any logic meant each module could be built, tested, and reasoned about independently.

```
Azure Blob Storage
      |  [Stage 1] extract.py
  DocumentRecord(text, metadata, source_type)
      |  [Stage 2] chunk.py
  ChunkRecord(chunk_id, text, chunk_index, metadata)
      |  [Stage 3] embed.py
  EmbeddedChunk(chunk_id, text, embedding, metadata)
      |  [Stage 4] index.py
  ChromaDB(id, document, embedding, metadata)
      |  [Stage 5] search.py
  SearchResult(rank, text, score, caption, metadata)
      |  [Stage 6] ingest.py
  Orchestration: extract -> chunk -> embed -> index
```

## 3. Stage-by-Stage Design Decisions

### Stage 1: Extraction (`extract.py`)

**Problem:** The corpus contains three document formats and two PDF subtypes (digital and scanned),
each requiring a different extraction path.

**Key decision: heuristic OCR routing**

Rather than attempting OCR on every PDF (slow) or assuming all PDFs have a text layer (incorrect
for scanned documents), scanned PDFs are detected by checking the average extracted characters per
page. If PyMuPDF returns fewer than 50 characters per page on average, the document is treated as
scanned and routed to pytesseract at 300 DPI.

- PyMuPDF extraction: approximately milliseconds per page
- Tesseract OCR at 300 DPI: approximately 2 to 5 seconds per page

This threshold-based routing ensures digital PDFs never pay the OCR cost.

**Key decision: Markdown stripping**

Raw Markdown is rendered to HTML via the `markdown` library, then stripped to plain text via
BeautifulSoup. This removes `#`, `**`, and `[link](url)` syntax that would otherwise pollute
embeddings and inflate BM25 term weights on structural tokens.

**Output:** Every document, regardless of format, becomes a `DocumentRecord` with `text`,
`page_count`, `source_type`, and `metadata` fields. Downstream stages are completely format-agnostic.

### Stage 2: Chunking (`chunk.py`)

**Problem:** Raw full-document text cannot be embedded or retrieved meaningfully. Documents must
be split into segments that are:

- Long enough to carry standalone context
- Short enough to embed precisely
- Semantically clean with no mid-sentence cuts
- Overlapping at boundaries to prevent answers from falling between chunks

The three candidate strategies and their tradeoffs:

| Approach | Pro | Con | Best for |
|---|---|---|---|
| Fixed-size character splits | Simple, fast, no dependencies | Cuts mid-sentence; destroys grammatical and semantic context at boundaries | Homogeneous plain-text where sentence integrity does not matter |
| Sentence-boundary splits (selected) | Clean units, token-aware, deterministic, no embedding calls at ingest | Variable chunk length can produce very short or very long chunks | Technical documents, manuals, structured prose |
| Semantic splits (embedding-based) | Chunks align with topic shifts; highest semantic coherence per chunk | One embedding call per sentence at ingest, making it 100 to 10,000x slower; non-deterministic | Long unstructured prose such as legal policies or research papers where topic shifts do not follow sentence counts |

**Key decision: SentenceSplitter with 512/50 parameters**

| Parameter | Value | Reasoning |
|---|---|---|
| `chunk_size` | 512 tokens | Matches `text-embedding-3-small` sweet spot; approximately 350 to 400 words of context |
| `chunk_overlap` | 50 tokens | Approximately 1 to 2 sentences; captures boundary-spanning information |
| Strategy | `sentence` (default) | Deterministic, fast, no embedding calls required |

**Key decision: semantic chunking as opt-in**

`SemanticSplitter` (topic-shift-based) is available via `CHUNKING_STRATEGY=semantic` but is off
by default. It requires one embedding call per sentence during ingestion, making it 100 to 10,000x
more expensive than `SentenceSplitter`. It is most useful for long, unstructured prose such as
legal policy documents where topic boundaries do not align with sentence counts.

**Key decision: minimum chunk size filter**

Chunks shorter than 30 characters are dropped. These are page numbers, section dividers, and
standalone headers that contribute noise to BM25 and vector indexes without containing retrievable
information.

**Output:** Each `ChunkRecord` carries full metadata inheritance from its parent `DocumentRecord`
plus `chunk_index` and `chunk_total`. The `chunk_id` follows the pattern `{blob_name}_{chunk_index}`,
making it deterministic and easy to trace.

### Stage 3: Embeddings (`embed.py`)

**Problem:** Dense vector representations must be generated for each chunk, while keeping the
pipeline runnable in environments where Azure credentials are not available.

**Key decision: two-tier embedding strategy**

The following table compares all candidates evaluated:

| Model | Provider | Dims | MTEB Retrieval | Size | Cost | Infra needed | Role in pipeline |
|---|---|---|---|---|---|---|---|
| `text-embedding-3-small` (selected) | Azure OpenAI | 1536 | Strong | API only | ~$0.02 / 1M tokens | Azure subscription | Primary |
| `text-embedding-3-large` | Azure OpenAI | 3072 | Best | API only | ~$0.13 / 1M tokens | Azure subscription | Overkill for ~10 docs |
| `text-embedding-ada-002` | Azure OpenAI | 1536 | Good | API only | ~$0.10 / 1M tokens | Azure subscription | Superseded by `3-small` |
| `all-MiniLM-L6-v2` (selected) | Hugging Face | 384 | Decent | ~90 MB | Free | None (CPU) | Local fallback |
| `all-mpnet-base-v2` | Hugging Face | 768 | Better | ~420 MB | Free | None (CPU) | Too large for a dev fallback |
| `bge-small-en-v1.5` | BAAI / HF | 384 | Slightly better than MiniLM | ~90 MB | Free | None (CPU) | Requires instruction prefix; added complexity |
| `embed-english-v3.0` | Cohere | 1024 | Strong | API only | ~$0.10 / 1M tokens | Cohere account | Good production alternative, outside Azure |

**Why `text-embedding-3-small`:**

- Outperforms the previous standard (`ada-002`) on MTEB retrieval benchmarks at approximately 5x lower cost
- 1536 dimensions provides fine-grained semantic resolution without the storage and compute cost of `3-large` (3072-dim, 2.6x more expensive; overkill for a ~10-doc corpus)
- Native to the Azure ecosystem: same tenant, same network, no data leaving the boundary
- Supports dimension reduction (truncatable to 256 or 512 dimensions) if latency becomes a constraint; `ada-002` does not

**Why `all-MiniLM-L6-v2` as the fallback:**

- Approximately 90 MB; downloads once, runs on CPU, no GPU required
- Best retrieval quality per MB at this size class on MTEB; the next step up (`all-mpnet-base-v2`) is 4.5x larger and 2 to 3x slower with marginal quality gain for development use
- Most downloaded sentence-transformer on Hugging Face; stable, well-tested, and widely understood

**Critical constraint: models cannot be mixed**

The two models produce vectors in completely different learned semantic spaces, not just different
lengths. Comparing a 1536-dim `text-embedding-3-small` vector to a 384-dim `all-MiniLM-L6-v2`
vector is meaningless. Switching models requires running `ingest.py --reset` to flush and
re-index. The `embedding_model` field is stored in every chunk's metadata as an audit trail.

**Provider abstraction**

Both models share the same `embed_batch(texts) -> list[list[float]]` interface. The rest of the
pipeline never calls either model directly; it calls `embed_chunks()` or `get_query_embedding()`.
Adding a third provider such as Cohere `embed-english-v3.0` requires only a new class and one
line in `_build_embedder()`.

**Key decision: batched embedding calls**

Chunks are embedded in batches of 32 (configurable via `EMBEDDING_BATCH_SIZE`). This reduces
HTTP round-trips and keeps each request within Azure OpenAI's 8192-token per-request limit.

**Output:** `EmbeddedChunk` extends `ChunkRecord` with `embedding: List[float]` and
`embedding_model: str` fields.

### Stage 4: Indexing (`index.py`)

**Problem:** Chunks and their embeddings must be stored in a way that supports fast
nearest-neighbour lookup and metadata filtering.

**Key decision: ChromaDB**

ChromaDB was chosen over Azure AI Search, Pinecone, Weaviate, Qdrant, and pgvector for this
implementation because it requires zero infrastructure, runs embedded in-process, and has a
clean Python API suited to a demo-scale pipeline.

| Feature | ChromaDB | Azure AI Search | Pinecone | Weaviate | Qdrant | pgvector |
|---|---|---|---|---|---|---|
| Infrastructure | None (embedded) | Managed Azure | Managed SaaS | Managed / self-host | Managed / self-host | PostgreSQL extension |
| Scale | ~1M vectors | Billions | Billions | Billions | Billions | ~10M practical |
| BM25 / keyword built-in | No | Yes (native hybrid) | No | Yes (BM25 module) | No | No (use pg full-text) |
| Metadata filtering | Yes | Yes | Yes | Yes | Yes | Yes (SQL WHERE) |
| Semantic reranking built-in | No | Yes (semantic ranker) | No | No | No | No |
| Hybrid search (single query) | No (manual RRF) | Yes | No (manual) | Yes | No (manual) | No (manual) |
| Cost | Free | ~$25/mo (Basic) | Free tier limited; ~$70/mo (Starter) | Free tier; ~$25/mo (Sandbox cloud) | Free tier; ~$25/mo (Cloud) | Free (infra cost only) |
| Azure ecosystem fit | Low | Native | Low | Low | Low | Low |
| Operational complexity | None | Low (managed) | Low (managed) | Medium | Medium | Low (if already on Postgres) |
| Production readiness | Dev / demo | Enterprise | Enterprise | Enterprise | Production | Production (small scale) |

**Why each alternative was ruled out:**

- **Azure AI Search:** the intended production target, but requires an active Azure subscription, a deployed search service, and significant setup time. The right swap for a real BMO deployment, not for a self-contained demo.
- **Pinecone:** fully managed and scales well, but has no built-in BM25 (hybrid search requires a separate keyword index), is not Azure-native, and adds vendor lock-in outside the Azure ecosystem.
- **Weaviate:** has native BM25 and a hybrid search module, but requires a running Docker container or a managed cloud account. Adds more operational overhead than justified for approximately 10 documents. A strong choice for Kubernetes-native deployments.
- **Qdrant:** strong performance and a clean API, but no built-in BM25, making it less suited for cases where keyword retrieval matters alongside vector search.
- **pgvector:** a sensible choice for teams already running PostgreSQL, but vector search performance degrades past ~1M vectors without careful indexing, and it has no BM25 or hybrid search primitives.

For a production BMO deployment, **Azure AI Search** is the natural replacement: it natively
supports hybrid search, integrated semantic reranking, and operates within the same Azure tenant
with no data leaving the boundary.

**Key decision: upsert semantics**

All writes use ChromaDB's `upsert` operation rather than `add`. This makes re-running `ingest.py`
idempotent; no duplicate chunks are created regardless of how many times the pipeline runs on the
same documents.

**Key decision: cosine distance**

All embedding vectors are L2-normalised before storage. For normalised vectors, cosine distance
equals 1 minus dot product, making cosine and inner-product distance equivalent. ChromaDB's
`hnsw:space=cosine` is set explicitly for correctness.

### Stage 5: Hybrid Search (`search.py`)

**Problem:** No single retrieval signal is sufficient on its own:

- Pure vector search misses exact keyword matches such as error codes, product names, and exact phrases
- Pure BM25 misses semantic paraphrases; for example, "won't turn on" and "device not powering up" share no tokens but mean the same thing

**Solution: four-layer retrieval pipeline**

```
Query
  |-- BM25 keyword search      -> sparse ranked list  (exact term matching)
  |-- Vector similarity search -> dense ranked list   (semantic matching)
  |-- RRF fusion               -> unified ranked list
       |-- Cross-encoder rerank -> final top-n        (joint query-document scoring)
            |-- Caption extraction -> top sentence per result
```

**Key decision: Reciprocal Rank Fusion (RRF) over weighted score fusion**

BM25 scores are unbounded positive floats; vector cosine similarities are in [-1, 1]. Linear
combination requires normalisation that is both query-dependent and fragile to outliers. RRF
avoids this entirely by operating on rank positions:

```
RRF(d) = sum of  1 / (k + rank_i(d))
```

With k=60 (from the original paper), a document ranked 1st contributes 1/61 (approximately 0.0164)
and a document ranked 20th contributes 1/80 (0.0125). The decay is gentle enough to reward
consistently good ranks across both signals without any per-query normalisation.

**Key decision: cross-encoder reranking on top-20 RRF candidates**

Bi-encoders (the embedding model used for vector search) score queries and documents independently.
Cross-encoders score (query, document) pairs jointly, reading both at the same time; this produces
much more accurate relevance signals but is significantly slower.

Running the cross-encoder on the full corpus is not feasible. Running it on the top-20 RRF
candidates bounds latency to approximately 50 to 200ms while still correcting ranking errors
introduced by RRF. The model used is `cross-encoder/ms-marco-MiniLM-L-6-v2`, trained on MS MARCO
passage ranking, a benchmark directly analogous to this retrieval task.

**Key decision: semantic captions via cross-encoder sentence scoring**

Rather than returning the full 512-token chunk as a snippet, the most relevant sentence is
extracted by scoring each sentence independently as a (query, sentence) pair with the
already-loaded cross-encoder. The highest-scoring sentence is returned as the caption. This
reuses the reranking model at no additional cost and directly mirrors Azure AI Search's
semantic captions feature.

**Output:** `SearchResult` dataclass with `rank`, `blob_name`, `text`, `score`, `rrf_score`,
`bm25_rank`, `vector_rank`, `caption`, and full `metadata`.

### Stage 6: Orchestration (`ingest.py`)

**Problem:** The five stages must be wired into a single runnable pipeline with sensible
defaults, progress visibility, and operational controls.

**Key decisions:**

- `--reset` flag flushes and recreates the ChromaDB collection before indexing
- `--blobs` flag allows targeted re-ingestion of specific documents without re-processing the full corpus
- `--strategy` flag switches between `sentence` and `semantic` chunking at runtime
- Progress is logged at every stage with document counts and timing
- Embedding batches are processed sequentially with per-batch logging so large ingestion runs show clear progress

## 4. Key Optimizations

| Concern | Decision |
|---|---|
| **Retrieval accuracy** | 4-layer pipeline: BM25 + vector + RRF + cross-encoder reranking |
| **OCR cost** | Tesseract only runs when PyMuPDF yields fewer than 50 characters per page |
| **Embedding cost** | Sentence splitter (no embedding calls at ingest) is the default; semantic splitter is opt-in |
| **Reranker latency** | Cross-encoder runs on top-20 candidates only, not the full corpus |
| **Score fusion stability** | RRF replaces fragile per-query score normalisation |
| **Context at boundaries** | 50-token overlap prevents boundary-spanning answers from being missed |
| **Developer experience** | Local embedding fallback means the full pipeline runs with zero paid services |
| **Idempotency** | Upsert-based indexing with deterministic chunk IDs |
| **Metadata richness** | Source, page number, folder, document type, and chunk position are preserved on every chunk |
| **Security** | All credentials are loaded via environment variables; `.env.example` is provided and `.env` is gitignored |

## 5. Known Trade-offs and Limitations

| Limitation | Impact | Mitigation at scale |
|---|---|---|
| ChromaDB is single-node in-memory HNSW | Not suitable for more than ~1M vectors | Replace with Azure AI Search or Pinecone |
| BM25 index is rebuilt from ChromaDB on every process start | Takes seconds for large collections | Persist the BM25 index or use Elasticsearch |
| BM25 ignores metadata filters | Out-of-filter results can appear in RRF fusion | Partition the BM25 corpus by document type |
| OCR quality depends on scan resolution | Low-quality scans produce garbled text | Use Azure Document Intelligence in production |
| Table extraction is unstructured | Table cells are extracted as flat text with no grid structure | Use Azure Document Intelligence for table-heavy documents |
| Two embedding models cannot be mixed | Switching models requires a full re-index | Version the ChromaDB collection name by model identifier |
| Reranker adds 50 to 200ms per query | Not suitable for sub-50ms SLA requirements | Use Cohere Rerank API (GPU-hosted) instead |

## 6. Out of Scope

The following concerns are intentionally outside the boundaries of this pipeline:

- **Authentication and authorisation:** no user-level access control on search results
- **Multi-tenancy:** the index is a single shared collection; no per-tenant partitioning
- **Document update detection:** changes to a blob are not automatically detected; re-ingest must be triggered manually
- **CI/CD and deployment:** no containerisation, health checks, or automated testing pipeline
- **Multilingual support:** OCR and embeddings are configured for English only

## 7. Production Migration Path

The module boundaries in this implementation were designed with future migration in mind.
Swapping any component requires changing only one file.

| Component | This implementation | Production replacement |
|---|---|---|
| Vector store | ChromaDB (local) | Azure AI Search |
| BM25 | `rank_bm25` (in-memory) | Azure AI Search built-in keyword search |
| Hybrid fusion | Manual RRF | Azure AI Search semantic ranker |
| Reranker | Local cross-encoder | Cohere Rerank API or Azure ML endpoint |
| PDF OCR | pytesseract | Azure Document Intelligence |
| Blob listing | `azure-storage-blob` sequential | Async parallel with `asyncio` and `aiohttp` |
| Embedding | Azure OpenAI batched | Azure OpenAI with parallel async batches |
