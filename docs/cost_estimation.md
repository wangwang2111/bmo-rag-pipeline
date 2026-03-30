# Production Cost Estimation

## Overview

This document estimates the monthly cost of running the BMO RAG pipeline using the full
production-grade Azure service stack. It covers three deployment scales, explains what drives
the cost at each tier, and provides concrete strategies to minimise spend without sacrificing
retrieval quality.

## Production Service Stack

| Component | Service | Unit pricing |
|---|---|---|
| Vector + keyword search | Azure AI Search | $73/mo (Basic), $250/mo (S1), $1,000/mo (S2) |
| Embeddings at ingest + query time | Azure OpenAI `text-embedding-3-small` | $0.02 per 1M tokens |
| OCR for scanned PDFs | Azure Document Intelligence (Read API) | $1.50 per 1,000 pages |
| Semantic reranking | Cohere Rerank API | $1.00 per 1,000 searches |
| Document storage | Azure Blob Storage (LRS) | $0.018 per GB/month |

## Scenario 1: Small Scale (Internal Team)

**Profile:** ~10 to 50 documents, ~1,000 queries/month, single department.

| Service | Usage | Monthly cost |
|---|---|---|
| Azure AI Search Basic | 1 index, < 2GB | $73.00 |
| Azure OpenAI embeddings | 50 docs x 20 chunks x 512 tokens (one-time ingest) + 1,000 queries x 30 tokens | < $0.01 |
| Azure Document Intelligence | ~50 scanned pages, one-time | < $0.10 |
| Cohere Rerank | 1,000 searches | $1.00 |
| Azure Blob Storage | ~100 MB documents | < $0.01 |
| **Total** | | **~$74/month** |

**Verdict:** Economical for internal tooling. The index service dominates cost even at low query
volume because it is priced as a reserved capacity tier, not per-request.

## Scenario 2: Medium Scale (Departmental)

**Profile:** ~500 documents, ~10,000 queries/month, multiple teams sharing one deployment.

| Service | Usage | Monthly cost |
|---|---|---|
| Azure AI Search Standard S1 | 1 index, ~5GB, higher throughput | $250.00 |
| Azure OpenAI embeddings | 500 docs x 20 chunks at ingest + 10,000 queries x 30 tokens | ~$0.12 |
| Azure Document Intelligence | ~500 scanned pages, periodic re-ingest | ~$0.75 |
| Cohere Rerank | 10,000 searches | $10.00 |
| Azure Blob Storage | ~2 GB documents | ~$0.04 |
| **Total** | | **~$261/month** |

**Verdict:** Reasonable for a department-level knowledge base. Cohere Rerank begins to show
meaningful cost at this volume but is still minor relative to the search service.

## Scenario 3: Large Scale (Enterprise)

**Profile:** ~5,000 documents, ~100,000 queries/month, organisation-wide deployment with
SLA requirements.

| Service | Usage | Monthly cost |
|---|---|---|
| Azure AI Search Standard S2 | Multiple indexes, ~100GB, high throughput | $1,000.00 |
| Azure OpenAI embeddings | 5,000 docs at ingest + 100,000 queries x 50 tokens | ~$1.20 |
| Azure Document Intelligence | ~5,000 scanned pages, periodic | ~$7.50 |
| Cohere Rerank | 100,000 searches | $100.00 |
| Azure Blob Storage | ~20 GB documents | ~$0.36 |
| **Total** | | **~$1,109/month** |

**Verdict:** Embedding and OCR costs remain negligible at scale. The two meaningful cost drivers
are the Azure AI Search tier and the Cohere Rerank volume. Optimising those two has the highest
impact.

## Is It Worth It?

The full production stack makes sense when the following conditions are true:

- **Data sensitivity requires staying within Azure** (same tenant, same compliance boundary)
- **Query volume is high enough** that retrieval quality differences translate to measurable productivity gains
- **The corpus changes frequently** and automated re-ingest is needed
- **SLA requirements exist** (managed services provide uptime guarantees that self-hosted ChromaDB cannot)

For a take-home demo or proof-of-concept, the current implementation (ChromaDB + local cross-encoder
+ `all-MiniLM-L6-v2` fallback) runs at **zero cost** with no Azure account required. The production
migration path exists when the project graduates to a real deployment.

## Cost Optimisation Strategies

### 1. Cache query embeddings

Every search query must be embedded before the vector lookup. Identical or near-identical queries
(e.g., repeated FAQ-style questions) produce the same embedding. Caching the embedding result in
Redis or an in-memory dictionary by query string eliminates redundant API calls.

**Savings:** Up to 80% of embedding API calls on typical enterprise knowledge-base query patterns
where users repeatedly ask the same small set of questions.

### 2. Cache frequent search results

For high-traffic queries, the full search result set can be cached end-to-end. A query arriving
within a short TTL window (e.g., 5 minutes) skips BM25, vector search, and reranking entirely.

**Savings:** Eliminates Cohere Rerank cost for repeated queries; reduces Azure AI Search compute units.

### 3. Skip reranking for high-confidence RRF results

If the top RRF candidate has a substantially higher score than the second candidate (indicating
clear consensus between BM25 and vector), the cross-encoder reranking step can be skipped. A
simple threshold check (e.g., `rrf_score[0] > 2 * rrf_score[1]`) identifies these cases.

**Savings:** Reduces Cohere Rerank API calls by an estimated 20 to 40% depending on corpus
homogeneity.

### 4. Use Azure AI Search's built-in semantic ranker instead of Cohere

Azure AI Search's semantic ranker is included in Standard S1 and above at no additional per-query
charge (up to 1,000 semantic queries/month on S1; higher on S2/S3). For deployments already
paying for Azure AI Search Standard, this replaces the Cohere Rerank cost entirely.

**Savings:** $10 to $100/month depending on query volume, at the cost of migrating the reranking
call from Cohere to the Azure AI Search semantic query parameter.

### 5. Use dimension reduction on embeddings

`text-embedding-3-small` supports truncating its 1536-dimensional output to 256 or 512 dimensions
with minimal quality loss on most retrieval benchmarks. Shorter vectors reduce storage in the
vector index and speed up nearest-neighbour computation.

**Savings:** Reduces Azure AI Search index storage size by 50 to 83%, which can defer an upgrade
from Basic to Standard for moderate-sized corpora.

### 6. Ingest only changed documents

The current pipeline supports `--blobs` to target specific files. Adding a last-modified timestamp
check against Azure Blob Storage metadata before re-embedding means only new or updated documents
incur embedding and Document Intelligence costs on each ingest run.

**Savings:** Eliminates re-embedding cost for unchanged documents on incremental ingest runs,
which is the dominant ingest cost pattern for stable knowledge bases.

### 7. Downgrade Azure AI Search tier during off-hours

Azure AI Search supports scaling down to Basic or Free during low-traffic windows (e.g., overnight
or weekends) and scaling back up before business hours. This requires automation via Azure CLI or
Terraform but can reduce the search service cost by 30 to 50% for workloads with predictable
usage patterns.

**Savings:** $75 to $125/month at Standard S1 assuming 50% off-hours reduction.

## Summary

| Scale | Monthly cost | Primary cost driver |
|---|---|---|
| Small (10-50 docs, 1K queries) | ~$74 | Azure AI Search Basic tier |
| Medium (500 docs, 10K queries) | ~$261 | Azure AI Search S1 tier |
| Large (5,000 docs, 100K queries) | ~$1,109 | Azure AI Search S2 + Cohere Rerank |

Embedding and OCR costs are negligible at all scales. The search index tier and reranking API
volume are the only levers that meaningfully change the monthly bill. Implementing query-level
caching and switching to Azure AI Search's built-in semantic ranker are the two highest-impact
optimisations available with minimal code changes.
