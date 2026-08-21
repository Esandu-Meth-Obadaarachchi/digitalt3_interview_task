"""Text embeddings for the dense half of hybrid retrieval (M8).

`all-MiniLM-L6-v2`, run locally. The toolkit tab notes that local embeddings
avoid rate limits entirely and are fast enough on a laptop, which matters here
because the LLM free tier is already the binding constraint: spending twenty
requests a day on embeddings would leave nothing for extraction.

Two implementations behind one interface, for the same reason the LLM layer has
two. `MiniLMEmbedder` is the real one. `HashingEmbedder` is deterministic,
needs no model download and no torch, and is what the test suite uses: loading
a transformer to assert that a search returns three results would put two
seconds on every test that touches retrieval.

The hashing embedder is not a mock. It produces genuine, stable vectors with
real cosine geometry, so the index, the search and the fusion are all exercised
for real. What it cannot do is understand meaning, which is exactly why the
retrieval quality numbers in the README come from a run against MiniLM and the
harness records which embedder produced them.
"""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from functools import lru_cache

import numpy as np

from app.config import Settings, get_settings

logger = logging.getLogger("agent.retrieval")


class Embedder(ABC):
    """Turns text into unit-length vectors."""

    name: str
    dimensions: int

    @abstractmethod
    def encode(self, texts: list[str]) -> np.ndarray:
        """Return an (n, dimensions) float32 array of unit-length vectors.

        Unit length by contract, so an inner-product index computes cosine
        similarity and nothing downstream has to normalise again or remember
        whether it already did.
        """

    def encode_one(self, text: str) -> np.ndarray:
        return self.encode([text])[0]

    def describe(self) -> str:
        return f"{self.name} ({self.dimensions}d)"


def _normalise(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    # A zero vector has no direction; leaving it at zero is better than
    # dividing by zero and producing NaNs that poison every later comparison.
    norms[norms == 0] = 1.0
    return (vectors / norms).astype("float32")


class MiniLMEmbedder(Embedder):
    """sentence-transformers/all-MiniLM-L6-v2, 384 dimensions, run locally."""

    def __init__(self, model_name: str) -> None:
        self.name = model_name
        self.dimensions = 384
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            logger.info("loading embedding model %s", self.name)
            self._model = SentenceTransformer(self.name)
            # Renamed in sentence-transformers 6. Both spellings are tried so the
            # pinned version and older ones both work, rather than pinning the
            # library to whichever one happens to be installed today.
            for attribute in ("get_embedding_dimension", "get_sentence_embedding_dimension"):
                getter = getattr(self._model, attribute, None)
                if getter is not None:
                    self.dimensions = getter()
                    break
        return self._model

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dimensions), dtype="float32")
        vectors = self._load().encode(
            texts, convert_to_numpy=True, normalize_embeddings=True, show_progress_bar=False
        )
        return vectors.astype("float32")


class HashingEmbedder(Embedder):
    """A deterministic embedder with no model behind it.

    Hashes word trigrams and unigrams into a fixed-width vector. Two texts
    sharing vocabulary land near each other, which is enough to exercise the
    index, the search and the fusion for real. It has no notion of meaning, so
    "postpone" and "defer" are unrelated to it, and any retrieval quality
    number produced with it would be meaningless. The harness records the
    embedder on every report so that cannot be missed.
    """

    def __init__(self, dimensions: int = 256) -> None:
        self.name = "hashing"
        self.dimensions = dimensions

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dimensions), dtype="float32")

        out = np.zeros((len(texts), self.dimensions), dtype="float32")
        for row, text in enumerate(texts):
            words = text.lower().split()
            features = words + [" ".join(words[i : i + 3]) for i in range(len(words) - 2)]
            for feature in features:
                digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "big") % self.dimensions
                sign = 1.0 if digest[4] % 2 else -1.0
                out[row, index] += sign
        return _normalise(out)


@lru_cache(maxsize=4)
def _cached(provider: str, model_name: str) -> Embedder:
    if provider == "hashing":
        return HashingEmbedder()
    return MiniLMEmbedder(model_name)


def get_embedder(settings: Settings | None = None) -> Embedder:
    """One function, the same shape as the LLM and tracker factories."""
    cfg = settings or get_settings()
    return _cached(cfg.embedding_provider, cfg.embedding_model)


def reset_embedder_cache() -> None:
    _cached.cache_clear()
