"""
evaluate.py
===========
Two-layer evaluation for the BMO RAG pipeline.

Layer 1 — Retrieval quality (no LLM required)
  Recall@K  — fraction of queries with the correct document in the top K results
  MRR       — Mean Reciprocal Rank; average of 1/rank of the first correct hit

Layer 2 — Answer quality (requires Azure OpenAI chat endpoint + RAGAS)
  Faithfulness     — does the generated answer stay within the retrieved context?
  Answer relevancy — does the generated answer actually address the question?

Both layers are independently runnable. Layer 2 adds three dependencies
(ragas, langchain-openai, datasets) and requires a chat deployment in Azure
OpenAI (set AZURE_OPENAI_CHAT_DEPLOYMENT in your .env file).

Usage
-----
  from evaluate import run_retrieval_eval, generate_answer, run_ragas_eval
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# ── Layer 1: Retrieval quality ────────────────────────────────────────────────

@dataclass
class RetrievalResult:
    """Per-query retrieval evaluation result."""

    query: str
    query_type: str
    expected_blob: str
    retrieved_blobs: list[str]
    reciprocal_rank: float
    """1 / rank of the first correct document; 0.0 if not retrieved."""
    hit_at: dict[int, bool]
    """hit_at[k] is True if the correct document appears in the top-k results."""


def _reciprocal_rank(retrieved_blobs: list[str], expected_blob: str) -> float:
    """Return 1/rank of the first matching blob, or 0.0 if not found."""
    for i, blob in enumerate(retrieved_blobs, start=1):
        if expected_blob in blob:
            return 1.0 / i
    return 0.0


def run_retrieval_eval(
    engine,
    ground_truth: list[tuple[str, str, str]],
    ks: list[int] = None,
) -> tuple[list[RetrievalResult], dict]:
    """
    Evaluate retrieval quality using Recall@K and MRR.

    Parameters
    ----------
    engine:
        Any search engine with a ``search(query, top_n) -> list[SearchResult]``
        method.  Works with both ``HybridSearchEngine`` and
        ``AzureAISearchEngine``.
    ground_truth:
        List of ``(query, expected_blob_substring, query_type)`` tuples.
        ``expected_blob_substring`` is matched as a substring of each retrieved
        ``blob_name`` (e.g. ``"error101.md"`` matches
        ``"troubleshooting/error101.md"``).
    ks:
        Values of K for Recall@K.  Defaults to ``[1, 3, 5]``.

    Returns
    -------
    Tuple of (per-query result list, summary dict).

    Summary keys: ``n_queries``, ``mrr``, ``recall@1``, ``recall@3``, etc.
    """
    if ks is None:
        ks = [1, 3, 5]

    results: list[RetrievalResult] = []
    top_k = max(ks)

    for query, expected_blob, query_type in ground_truth:
        hits = engine.search(query, top_n=top_k)
        retrieved = [r.blob_name for r in hits]
        rr = _reciprocal_rank(retrieved, expected_blob)
        hit_at = {k: any(expected_blob in b for b in retrieved[:k]) for k in ks}

        results.append(RetrievalResult(
            query=query,
            query_type=query_type,
            expected_blob=expected_blob,
            retrieved_blobs=retrieved,
            reciprocal_rank=rr,
            hit_at=hit_at,
        ))
        logger.debug(
            "Q=%r  RR=%.3f  %s",
            query, rr,
            "  ".join(f"R@{k}={'Y' if h else 'N'}" for k, h in hit_at.items()),
        )

    n = len(results)
    summary: dict = {
        "n_queries": n,
        "mrr": round(sum(r.reciprocal_rank for r in results) / n, 4),
        **{
            f"recall@{k}": round(sum(r.hit_at[k] for r in results) / n, 4)
            for k in ks
        },
    }
    logger.info("Retrieval eval complete: %s", summary)
    return results, summary


# ── Layer 2a: Answer generation ───────────────────────────────────────────────

def generate_answer(
    query: str,
    contexts: list[str],
    *,
    deployment: Optional[str] = None,
    max_tokens: int = 300,
) -> str:
    """
    Generate a grounded answer from retrieved contexts using Azure OpenAI.

    The model is instructed to answer only from the provided context.  This
    strict grounding makes faithfulness scoring meaningful: if the answer
    introduces facts not present in the context, RAGAS will flag it.

    Parameters
    ----------
    query:
        The user's question.
    contexts:
        Retrieved chunk texts, ordered by rank.
    deployment:
        Azure OpenAI chat deployment name (e.g. ``gpt-4o``, ``gpt-35-turbo``).
        Defaults to the ``AZURE_OPENAI_CHAT_DEPLOYMENT`` env var.
    max_tokens:
        Maximum tokens in the generated answer.

    Returns
    -------
    Generated answer string.
    """
    try:
        from openai import AzureOpenAI
    except ImportError as exc:
        raise ImportError("pip install openai>=1.10.0") from exc

    client = AzureOpenAI(
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01"),
    )
    chat_deployment = deployment or os.environ["AZURE_OPENAI_CHAT_DEPLOYMENT"]

    context_block = "\n\n---\n\n".join(contexts)
    system_prompt = (
        "You are a technical support assistant. Answer the question using ONLY "
        "the context provided below. If the answer cannot be found in the context, "
        "respond with: 'The context does not contain enough information to answer "
        "this question.'\n\n"
        f"Context:\n{context_block}"
    )

    response = client.chat.completions.create(
        model=chat_deployment,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": query},
        ],
        max_tokens=max_tokens,
        temperature=0,   # deterministic for reproducible evaluation
    )
    answer = response.choices[0].message.content.strip()
    logger.debug("Generated answer for %r: %s", query, answer[:100])
    return answer


# ── Layer 2b: RAGAS evaluation ────────────────────────────────────────────────

def run_ragas_eval(
    queries: list[str],
    contexts_list: list[list[str]],
    answers: list[str],
    ground_truths: Optional[list[str]] = None,
) -> dict:
    """
    Score answer quality using RAGAS.

    Metrics computed:
      ``faithfulness``      — what fraction of answer claims are grounded in the
                             retrieved context? (1.0 = fully grounded)
      ``answer_relevancy``  — how well does the answer address the question?
                             (1.0 = perfectly on-topic)
      ``context_recall``    — fraction of ground-truth information covered by the
                             retrieved context (only if ``ground_truths`` provided)

    Parameters
    ----------
    queries:
        List of search questions.
    contexts_list:
        Parallel list of retrieved context lists (one list of strings per query).
    answers:
        Parallel list of generated answers (one per query).
    ground_truths:
        Optional expected answer strings.  Required only for ``context_recall``.

    Returns
    -------
    Dict mapping metric name to mean score across all samples.
    """
    try:
        from datasets import Dataset
        from ragas import evaluate
        from ragas.metrics import answer_relevancy, faithfulness
    except ImportError as exc:
        raise ImportError(
            "pip install ragas==0.1.21 langchain-openai==0.1.8 datasets==2.19.0"
        ) from exc

    _configure_ragas_for_azure()

    data: dict = {
        "question": queries,
        "answer":   answers,
        "contexts": contexts_list,
    }
    metrics = [faithfulness, answer_relevancy]

    if ground_truths is not None:
        from ragas.metrics import context_recall
        data["ground_truth"] = ground_truths
        metrics.append(context_recall)

    dataset = Dataset.from_dict(data)
    result = evaluate(dataset, metrics=metrics)
    scores = {k: round(float(v), 4) for k, v in result.items()}
    logger.info("RAGAS scores: %s", scores)
    return scores


def _configure_ragas_for_azure() -> None:
    """
    Wire RAGAS metrics to the Azure OpenAI chat model and embeddings.

    RAGAS uses LangChain adapters internally. This function injects
    ``AzureChatOpenAI`` (for statement decomposition and verdict scoring)
    and ``AzureOpenAIEmbeddings`` (for answer relevancy cosine similarity)
    into the metric objects before evaluation runs.

    Required env vars
    -----------------
    AZURE_OPENAI_CHAT_DEPLOYMENT   Chat model deployment (e.g. gpt-4o)
    AZURE_OPENAI_ENDPOINT          Azure OpenAI resource endpoint
    AZURE_OPENAI_API_KEY           API key
    AZURE_OPENAI_DEPLOYMENT_NAME   Embedding deployment (for answer relevancy)
    """
    try:
        from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings
        from ragas.llms import LangchainLLMWrapper
        from ragas.metrics import answer_relevancy, faithfulness
    except ImportError as exc:
        raise ImportError(
            "pip install langchain-openai==0.1.8 ragas==0.1.21"
        ) from exc

    endpoint   = os.environ["AZURE_OPENAI_ENDPOINT"]
    api_key    = os.environ["AZURE_OPENAI_API_KEY"]
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
    chat_deploy  = os.environ["AZURE_OPENAI_CHAT_DEPLOYMENT"]
    embed_deploy = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "text-embedding-3-small")

    ragas_llm = LangchainLLMWrapper(
        AzureChatOpenAI(
            azure_deployment=chat_deploy,
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
            temperature=0,
        )
    )
    ragas_embeddings = AzureOpenAIEmbeddings(
        azure_deployment=embed_deploy,
        azure_endpoint=endpoint,
        api_key=api_key,
        api_version=api_version,
    )

    # Inject into each metric that uses an LLM or embeddings
    faithfulness.llm = ragas_llm
    answer_relevancy.llm = ragas_llm
    answer_relevancy.embeddings = ragas_embeddings

    logger.debug(
        "RAGAS configured: chat=%s  embed=%s", chat_deploy, embed_deploy
    )


# ── CLI smoke-test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    print("evaluate.py — smoke test (retrieval layer only, no Azure calls)")
    print("=" * 60)

    # Minimal fake engine for offline testing
    class _FakeEngine:
        def search(self, query, top_n=5):
            from dataclasses import dataclass
            @dataclass
            class R:
                blob_name: str
            return [R("troubleshooting/error101.md"), R("manuals/deviceA.pdf")]

    gt = [
        ("error 101",  "error101.md", "exact keyword"),
        ("device A manual", "deviceB.pdf", "exact keyword"),  # intentional miss
    ]

    res, summary = run_retrieval_eval(_FakeEngine(), gt, ks=[1, 2])
    print(f"MRR:       {summary['mrr']}")
    print(f"Recall@1:  {summary['recall@1']}")
    print(f"Recall@2:  {summary['recall@2']}")
    print("\nSmoke-test passed.")
