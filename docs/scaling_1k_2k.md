# Scaling to 1,000-2,000 Documents

Notes on what actually needs to change when the corpus grows from the demo (10 docs) to a realistic internal deployment.

## Corpus size estimate

- Average document: ~20 chunks x 512 tokens
- 1,000 docs = ~20,000 chunks
- 2,000 docs = ~40,000 chunks

At 40k chunks, ChromaDB, BM25, and the cross-encoder all still work fine. Nothing technically breaks. The issues are ingest time and cold-start behaviour, not search quality.

## What breaks or slows down

**Extraction.** A sequential loop over 2,000 documents at ~2s per digital PDF takes about an hour. Scanned PDFs with OCR push that much higher. This is the main bottleneck. Fix: `asyncio` with the async Azure SDK for downloads (I/O-bound) and `ProcessPoolExecutor` for OCR (CPU-bound). Realistic speedup: 5-10x depending on core count.

**BM25 cold start.** 40k tokenised chunks take ~15-30 seconds to load into memory on process start and consume around 500MB RAM. Fine for a long-running server (one-time cost) but noticeable for short-lived jobs. Fix: serialize the BM25 index to disk after build and load it on startup instead of rebuilding from ChromaDB every time.

**Embedding rate limits.** 40k chunks at 32 per batch = 1,250 API calls. At ~75ms each that's about 94 seconds of API time, but at this volume you will occasionally hit Azure OpenAI TPM limits. Worth adding retry logic with exponential backoff if not already in place.

## What stays fine

- ChromaDB handles up to ~1M vectors. 40k is well within range.
- Cross-encoder always scores only top-20 candidates regardless of corpus size.
- Search latency stays roughly constant. ChromaDB HNSW is O(log n) and BM25 over 40k chunks runs in under 10ms.
- Overall search latency should remain around 185ms.

## Cheapest production path

Skip Fabric entirely. Run the existing pipeline on a small Azure VM.

| Item | Cost |
|---|---|
| Azure B2s VM (2 vCPU, 4GB RAM) | ~$30/mo |
| Azure OpenAI embeddings, one-time ingest of 40k chunks | ~$0.04 |
| Re-embed on updates, ~20% new chunks per run | <$0.01/run |
| ChromaDB, BM25, cross-encoder | Free |
| **Total** | **~$30/mo** |

If the VM doesn't need to run 24/7, Azure Container Instances are even cheaper: pay per second of execution (~$0.000012/vCPU-second). A full re-ingest of 2,000 docs would cost roughly $0.10-0.50.

## When to upgrade beyond this

The only real reason to move to Azure AI Search ($73/mo Basic) is concurrent query load, not document count. If multiple users are querying simultaneously and ChromaDB on a single VM becomes a bottleneck, that's the trigger. At 1,000-2,000 docs with light query traffic, the $30 VM path is sufficient.

Fabric makes sense only if the organisation already pays for Fabric capacity for other workloads (Power BI, other pipelines). In that case the RAG ingest runs against existing capacity at near-zero incremental cost. As a standalone deployment for this corpus size, Fabric's minimum practical SKU (F4, ~$524/mo) is not justified.

## Concrete change list

| Component | Change needed |
|---|---|
| Extraction loop | Parallelise: `asyncio` for downloads, `ProcessPoolExecutor` for OCR |
| BM25 cold start | Serialize index to disk, load on startup |
| Embedding | Add retry + exponential backoff for TPM limits |
| ChromaDB | No change needed |
| Cross-encoder reranking | No change needed |
| Search latency | No change expected |
