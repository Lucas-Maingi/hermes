"""Tenant-scoped retrieval-augmented generation with BM25 and vector fusion."""

from __future__ import annotations

import math
import re
from collections import Counter

import httpx

from hermes.platform import PlatformStore

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.I)
_CHUNK_SIZE = 1200
_CHUNK_OVERLAP = 180
_RRF_K = 60


def chunk_text(text: str, *, size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return []
    chunks = []
    start = 0
    while start < len(cleaned):
        end = min(start + size, len(cleaned))
        if end < len(cleaned):
            boundary = cleaned.rfind(" ", start + size // 2, end)
            if boundary > start:
                end = boundary
        chunks.append(cleaned[start:end].strip())
        if end == len(cleaned):
            break
        start = max(start + 1, end - overlap)
    return chunks


def _tokens(text: str) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(text)]


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


class EmbeddingClient:
    """OpenAI-compatible embedding endpoint. Calls are opt-in per merchant."""

    def __init__(self, *, api_key: str, base_url: str, model: str, timeout: float = 30.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = httpx.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": texts},
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()["data"]
        return [row["embedding"] for row in sorted(data, key=lambda row: row["index"])]


class HybridRetriever:
    """BM25 lexical ranking plus optional cosine vector ranking, fused by RRF."""

    def __init__(self, store: PlatformStore, *, embedding_client: EmbeddingClient | None = None):
        self.store = store
        self.embedding_client = embedding_client

    def index_document(self, business_id: str, title: str, text: str) -> str:
        chunks = chunk_text(text)
        if not chunks:
            raise ValueError("Document text cannot be empty")
        vectors = self.embedding_client.embed(chunks) if self.embedding_client else [None] * len(chunks)
        return self.store.add_document(
            business_id,
            title,
            list(zip(chunks, vectors)),
        )

    def search(self, business_id: str, query: str, *, limit: int = 5) -> list[dict]:
        if not query.strip():
            return []
        candidates = self.store.list_chunks(business_id)
        if not candidates:
            return []

        query_tokens = _tokens(query)
        lexical_ranking = self._bm25(candidates, query_tokens)
        lexical_scores = dict(lexical_ranking)
        vector_ranking: list[tuple[str, float]] = []
        vector_scores: dict[str, float] = {}
        if self.embedding_client:
            query_vector = self.embedding_client.embed([query])[0]
            vector_ranking = sorted(
                (
                    (item["id"], _cosine(query_vector, item["embedding"]))
                    for item in candidates
                    if item["embedding"] is not None
                ),
                key=lambda pair: pair[1],
                reverse=True,
            )
            vector_scores = dict(vector_ranking)

        rankings = [lexical_ranking]
        if vector_ranking:
            rankings.append(vector_ranking)
        fused: Counter[str] = Counter()
        for ranking in rankings:
            for rank, (chunk_id, score) in enumerate(ranking, start=1):
                fused[chunk_id] += 1 / (_RRF_K + rank)

        by_id = {item["id"]: item for item in candidates}
        results = []
        for chunk_id, rrf_score in fused.most_common(limit):
            item = by_id[chunk_id]
            item["lexical_score"] = lexical_scores.get(chunk_id, 0.0)
            item["vector_score"] = vector_scores.get(chunk_id, 0.0)
            if item["lexical_score"] <= 0 and item["vector_score"] <= 0:
                continue
            item["score"] = rrf_score
            results.append(item)
        return results

    @staticmethod
    def _bm25(candidates: list[dict], query_tokens: list[str]) -> list[tuple[str, float]]:
        if not query_tokens:
            return []
        tokenized = [_tokens(item["text"]) for item in candidates]
        document_frequency = Counter(
            token for tokens in tokenized for token in set(tokens)
        )
        average_length = sum(map(len, tokenized)) / max(1, len(tokenized))
        total_documents = len(candidates)
        scores: list[tuple[str, float]] = []
        for item, tokens in zip(candidates, tokenized):
            frequencies = Counter(tokens)
            length = len(tokens)
            score = 0.0
            for token in query_tokens:
                frequency = frequencies[token]
                if not frequency:
                    continue
                idf = math.log(
                    1 + (total_documents - document_frequency[token] + 0.5)
                    / (document_frequency[token] + 0.5)
                )
                denominator = frequency + 1.2 * (1 - 0.75 + 0.75 * length / max(1, average_length))
                score += idf * frequency * 2.2 / denominator
            scores.append((item["id"], score))
        return sorted(scores, key=lambda pair: pair[1], reverse=True)


def build_retriever(store: PlatformStore, business_id: str) -> HybridRetriever:
    integrations = store.integrations(business_id)
    settings = integrations["llm"]
    api_key = settings.get("embedding_api_key", "")
    if not api_key or not integrations["embedding_consent"]:
        return HybridRetriever(store)
    base_url = settings.get("embedding_base_url", "https://api.openai.com/v1")
    model = settings.get("embedding_model", "text-embedding-3-small")
    return HybridRetriever(
        store,
        embedding_client=EmbeddingClient(api_key=api_key, base_url=base_url, model=model),
    )
