from hermes.platform import PlatformStore
from hermes.rag import HybridRetriever


class FixedEmbeddings:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [
            [0.0, 1.0]
            if text.lower() == "returns" or "banana" in text.lower()
            else [1.0, 0.0]
            for text in texts
        ]


def test_hybrid_search_combines_lexical_and_vector_candidates(tmp_path):
    store = PlatformStore(tmp_path / "rag.db")
    retriever = HybridRetriever(store, embedding_client=FixedEmbeddings())
    retriever.index_document("shop-a", "Returns", "Returns are accepted within three days.")
    retriever.index_document("shop-a", "Banana", "Banana products are available.")

    results = retriever.search("shop-a", "returns", limit=5)

    assert {result["title"] for result in results} == {"Returns", "Banana"}
    banana = next(result for result in results if result["title"] == "Banana")
    assert banana["lexical_score"] == 0
    assert banana["vector_score"] > 0
    assert retriever.search("shop-b", "returns", limit=5) == []
