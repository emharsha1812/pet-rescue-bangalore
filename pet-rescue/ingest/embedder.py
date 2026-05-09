"""Embedding helper that calls EIS via the Elasticsearch inference API.

For ingest: you don't need this. Indexing a document with `description` or `body`
automatically populates the `semantic_text` field (via copy_to) and EIS embeds it.

This module is for cases where you need an explicit embedding vector — e.g.
running a raw knn query or inspecting the vector for a document.
"""
from backend.es_client import get_es

EIS_INFERENCE_ID = ".jina-embeddings-v5-text-small"


def embed(texts: list[str]) -> list[list[float]]:
    """Return one embedding vector per input text, via EIS."""
    es = get_es()
    resp = es.inference.inference(
        inference_id=EIS_INFERENCE_ID,
        body={"input": texts},
    )
    return [item["embedding"] for item in resp["text_embedding"]]


def embed_one(text: str) -> list[float]:
    return embed([text])[0]
