# BMO RAG Pipeline

A production-grade **Azure ETL & Retrieval-Augmented Search (RAG)** pipeline that extracts documents from Azure Blob Storage, chunks and embeds them, stores them in a vector store, and serves hybrid search (BM25 + vector + semantic reranking).

The default vector backend is **ChromaDB** (local, zero infrastructure). Set `VECTOR_BACKEND=azure_ai_search` to switch to **Azure AI Search**, which provides native hybrid search, RRF fusion, and semantic reranking in a single managed service call.

## Table of Contents

- [Architecture](#architecture)
- [Folder Structure](#folder-structure)
- [Setup](#setup)
- [Running the Pipeline](#running-the-pipeline)
- [Module Quick Reference](#module-quick-reference)
- [Performance](#performance)
- [Assumptions](#assumptions)
- [Known Limitations](#known-limitations)
- [Development](#development)
- [Further Reading](#further-reading)

## Further Reading

| Document | Description |
|---|---|
| [Architecture decisions](docs/architecture.md) | Stage-by-stage design decisions, model selection tables, trade-offs, scalability, and production migration path |
| [Cost estimation](docs/cost_estimation.md) | Production cost breakdown across three scale scenarios and optimisation strategies |
| [Microsoft Fabric architecture](docs/fabric_architecture.md) | How to migrate this pipeline to Microsoft Fabric and Azure AI Search for a production BMO deployment |

## Architecture

```mermaid
flowchart TD
    A[Azure Blob Storage\nPDFs · Markdown · TXT] --> B[extract.py\nPyMuPDF · OCR · md · txt]
    B --> C[chunk.py\nSentenceSplitter · SemanticSplitter\nchunk_size=512 overlap=50]
    C --> D[embed.py\nAzure OpenAI text-embedding-3-small\nor local all-MiniLM-L6-v2]
    D --> E[index.py\nChromaDB persistent\ncosine distance · upsert]

    subgraph Search["search.py — Hybrid Retrieval"]
        direction LR
        Q[Query] --> QE[Embed query]
        Q --> BM[BM25 keyword search\nrank_bm25]
        QE --> VS[Vector similarity\nChromaDB]
        BM --> RRF[RRF Fusion\nReciprocal Rank Fusion]
        VS --> RRF
        RRF --> RE[Cross-encoder reranking\nms-marco-MiniLM-L-6-v2]
        RE --> CAP[Semantic caption\nextraction]
        CAP --> OUT[Top-N results\n+ metadata + captions]
    end

    E --> Search
```

## Folder Structure

```
bmo_1st_project/
├── src/
│   ├── extract.py     # Azure Blob → DocumentRecord (PDF/MD/TXT)
│   ├── chunk.py       # DocumentRecord → ChunkRecord list
│   ├── embed.py       # ChunkRecord → EmbeddedChunk (with vectors)
│   ├── index.py       # EmbeddedChunk → vector store (ChromaDB or Azure AI Search)
│   ├── ingest.py      # Orchestration: extract→chunk→embed→index
│   ├── search.py      # Hybrid search: BM25+vector+rerank
│   └── evaluate.py    # Recall@K, MRR, answer generation, RAGAS scoring
├── notebooks/
│   └── demo.ipynb     # End-to-end walkthrough with visualisations
├── docs/
│   ├── README.md      # This file
│   ├── architecture.md
│   ├── cost_estimation.md
│   └── fabric_architecture.md
├── sample_data/       # Synthetic test documents (mirrors Azure container layout)
│   ├── manuals/       #   deviceA.pdf, deviceB.pdf, deviceC_scanned.pdf, deviceD.pdf
│   ├── troubleshooting/ # error101.md, error102.md, error200.md
│   └── policies/      #   security.txt, data_retention.txt, incident_response.txt
├── _generate_samples.py # Script that generated the synthetic sample data
├── .env.example       # Environment variable template
└── requirements.txt   # Pinned dependencies
```

> **Sample data**: `sample_data/` contains synthetic documents that mirror the expected container layout (`manuals/`, `troubleshooting/`, `policies/`). These files were uploaded to an **Azure Data Lake Storage Gen2** container and the full pipeline was tested end-to-end against it. ADLS Gen2 is fully compatible with the `azure-storage-blob` SDK used here: the same connection string, `BlobServiceClient`, and `list_blobs()` / `download_blob()` calls work identically against both ADLS Gen2 and standard Blob Storage. To use your own documents, upload them to your container in the same folder structure and point `AZURE_STORAGE_CONTAINER_NAME` at it.

## Setup

### 1. Clone and create a virtual environment

```bash
git clone <repo>
cd bmo_1st_project
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. System dependencies (for OCR)

```bash
# macOS
brew install tesseract poppler

# Ubuntu / Debian
sudo apt-get install tesseract-ocr poppler-utils

# Windows
# Install Tesseract: https://github.com/UB-Mannheim/tesseract/wiki
# Install Poppler:  https://github.com/oschwartz10612/poppler-windows/releases
```

### 3. Configure environment variables

```bash
cp .env.example .env
# Edit .env and fill in your Azure credentials
```

Required variables:

| Variable | Description |
|---|---|
| `AZURE_STORAGE_CONNECTION_STRING` | Full Azure Storage connection string **or** use account name+key below |
| `AZURE_STORAGE_ACCOUNT_NAME` | Storage account name (if not using connection string) |
| `AZURE_STORAGE_ACCOUNT_KEY` | Storage account key (if not using connection string) |
| `AZURE_STORAGE_CONTAINER_NAME` | Blob container name (default: `documents`) |
| `AZURE_OPENAI_API_KEY` | Azure OpenAI API key (optional, falls back to local model) |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI endpoint URL |
| `AZURE_OPENAI_DEPLOYMENT_NAME` | Embedding deployment name (default: `text-embedding-3-small`) |

Optional variables (have sensible defaults):

| Variable | Default | Description |
|---|---|---|
| `VECTOR_BACKEND` | `chroma` | Vector store backend: `chroma` or `azure_ai_search` |
| `CHROMA_PERSIST_DIR` | `./chroma_db` | Path for ChromaDB storage (used when `VECTOR_BACKEND=chroma`) |
| `CHUNK_SIZE` | `512` | Token chunk size |
| `CHUNK_OVERLAP` | `50` | Token overlap between chunks |
| `TOP_N_RESULTS` | `5` | Default search result count |
| `EMBEDDING_BATCH_SIZE` | `32` | Embedding API batch size |
| `LOG_LEVEL` | `INFO` | Python logging level |

Azure AI Search variables (required when `VECTOR_BACKEND=azure_ai_search`):

| Variable | Default | Description |
|---|---|---|
| `AZURE_SEARCH_ENDPOINT` | | `https://<service>.search.windows.net` |
| `AZURE_SEARCH_KEY` | | Admin API key |
| `AZURE_SEARCH_INDEX_NAME` | `bmo-rag-chunks` | Index name |
| `AZURE_SEARCH_VECTOR_DIMS` | `1536` | Embedding dimensions (`384` for local fallback) |

### 4. Verify installation

```bash
python -c "import fitz, chromadb, rank_bm25, sentence_transformers, llama_index, azure.storage.blob, openai; print('All dependencies OK')"
```

## Running the Pipeline

### Full ingest (all documents)

```bash
python src/ingest.py
```

### Ingest specific blobs

```bash
python src/ingest.py --blobs manuals/deviceA.pdf troubleshooting/error101.md
```

### Reset collection and re-index

```bash
python src/ingest.py --reset
```

### Use semantic chunking (slower, higher quality)

```bash
python src/ingest.py --strategy semantic
```

### Search

```bash
python src/search.py "What causes error 101?"
python src/search.py "device configuration" --top-n 10 --source-type pdf_digital
python src/search.py "security policy" --full-text
```

### Run the notebook

```bash
jupyter notebook notebooks/demo.ipynb
```

## Module Quick Reference

### `extract.py`

```python
from extract import extract_all_documents, _build_container_client

container = _build_container_client()
docs = extract_all_documents(container)
# Returns List[DocumentRecord]
```

### `chunk.py`

```python
from chunk import chunk_documents

chunks = chunk_documents(docs, strategy='sentence')  # or 'semantic'
# Returns List[ChunkRecord]
```

### `embed.py`

```python
from embed import embed_chunks, get_query_embedding

embedded = embed_chunks(chunks)
query_vec = get_query_embedding("my search query")
```

### `index.py`

```python
from index import get_indexer

# Uses VECTOR_BACKEND env var to select ChromaDB or Azure AI Search
indexer = get_indexer()
stats = indexer.index_chunks(embedded)
```

### `search.py`

```python
from search import search

results = search("error 101 resolution", top_n=5)
for r in results:
    print(r.rank, r.blob_name, r.caption)
```

## Performance

Measured on a 10-document corpus (42 chunks) running on CPU (no GPU), using Azure OpenAI `text-embedding-3-small` (1536-dim) and `ms-marco-MiniLM-L-6-v2` cross-encoder.

### Search latency

| Stage | Time (ms) | Notes |
|---|---|---|
| Query embedding | 75.1 | Azure OpenAI API round-trip |
| BM25 keyword search | 0.2 | In-memory index, O(n chunks) |
| Vector similarity (ChromaDB) | 5.1 | Cosine distance over 42 chunks |
| RRF fusion | 0.0 | Pure Python, negligible |
| Cross-encoder reranking | 102.5 | Dominant cost; 20 candidates scored |
| Caption extraction | 0.5 | Token-overlap scoring, no model inference |
| **Total** | **185.7** | |

Cross-encoder reranking is the dominant bottleneck. For sub-50ms production latency, replace with [Cohere Rerank API](https://cohere.com/rerank) or cache reranker scores for frequent queries.

### Retrieval quality: Recall@K and MRR

Evaluated on 20 ground-truth queries spanning 4 query types across all 10 documents.

| | Recall@1 | Recall@3 | Recall@5 | MRR |
|---|---|---|---|---|
| Exact keyword | 100% | 100% | 100% | 1.000 |
| Semantic paraphrase | 100% | 100% | 100% | 1.000 |
| Policy retrieval | 75% | 75% | 75% | 0.750 |
| OCR / scanned PDF | 100% | 100% | 100% | 1.000 |
| **Overall** | **95%** | **95%** | **95%** | **0.950** |

The one persistent miss ("steps to contain a ransomware breach") is a known multi-hop reasoning gap. See [Known Limitations](#known-limitations) below.

### Answer quality: RAGAS

Evaluated on a 5-query sample (one per difficulty level) using `gpt-5-chat` as the answer generator and LLM-as-judge. Context retrieved via the hybrid search engine (top-3 chunks per query).

| Metric | Score | Interpretation |
|---|---|---|
| Faithfulness | 1.000 | All answer claims are grounded in retrieved context — no hallucination detected |
| Answer relevancy | 0.720 | Answers are on-topic; score is dampened by the small corpus (10 docs) — a larger document set with richer per-query context would push this above 0.85 |

Perfect faithfulness confirms the pipeline is not hallucinating. Answer relevancy is expected to be below 0.85 on a 10-document corpus — with more documents, retrieved context is richer and answers are more directly on-topic without needing to paraphrase.

## Assumptions

1. **Container structure**: Documents are organised in subfolders (`manuals/`, `troubleshooting/`, `policies/`) but the pipeline processes all blobs regardless of folder.
2. **Language**: All documents are English (Tesseract OCR configured for `eng`).
3. **PDF scan detection**: A page with fewer than 50 characters (on average) is considered scanned. This threshold works well for technical documents but may need tuning for dense tables.
4. **Embedding dimensions**: Azure OpenAI `text-embedding-3-small` produces 1536-dim vectors; the local fallback produces 384-dim vectors. The two cannot be mixed in the same index. If switching embedding models, run `python src/ingest.py --reset`. When using Azure AI Search, also set `AZURE_SEARCH_VECTOR_DIMS` to match the model.
5. **BM25 index is in-memory**: When using the ChromaDB backend, the BM25 index is rebuilt from the collection on each process start. For large collections (>100K chunks), switch to `VECTOR_BACKEND=azure_ai_search`, which provides a managed keyword index with no cold-start cost.

## Known Limitations

- **Scanned PDF quality**: OCR accuracy depends on scan quality and DPI. 300 DPI is the default; lower-quality scans may produce garbled text.
- **Table extraction**: PyMuPDF extracts table cells as plain text without structure. For table-heavy documents, consider Azure Document Intelligence.
- **Multilingual documents**: The pipeline is configured for English. Multi-language support requires setting `lang` in pytesseract and a multilingual embedding model.
- **BM25 + metadata filter mismatch**: BM25 searches the entire corpus; vector search respects the `filter_metadata` parameter. When a metadata filter is active, BM25 candidates from outside the filter may appear in RRF fusion. A production system would push BM25 inside the metadata-partitioned space.
- **BM25 text/metadata lookups**: Originally O(n) `list.index()` scans - which scans the list from the beginning until it finds a match (~800k string comparisons per query at 20k chunks). This has been fixed by adding a `_id_to_index: dict[str, int]` in `BM25Index` built once at index load time, reducing all lookups to O(1).
- **Reranker latency**: The cross-encoder adds ~100ms per query on CPU. For latency-sensitive applications, use Cohere Rerank API instead.
- **Multi-hop queries**: The pipeline retrieves in a single pass, embedding the query once and ranking chunks by direct similarity. This fails when answering a question requires chaining two separate chunks. For example, "steps to contain a ransomware breach" requires first linking "ransomware" to a P1 incident definition (Chunk A), then following that to the containment procedure section (Chunk B). Chunk B never mentions "ransomware" so it scores low and is not retrieved. The fix is query expansion with an LLM (HyDE, step-back prompting, or ReAct) to rewrite the query before retrieval, which is the standard motivation for agentic RAG architectures.

## Development

### Run individual module smoke-tests

```bash
python src/extract.py   # requires Azure credentials
python src/chunk.py     # fully offline, uses synthetic data
python src/embed.py     # uses local model fallback if no Azure OpenAI key
python src/index.py     # uses temp ChromaDB dir
```
