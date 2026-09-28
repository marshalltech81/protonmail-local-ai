"""Deterministic, provider-free embedder for the retrieval baseline.

Hashes each word and each padded character trigram into a fixed
4096-slot vector (signed feature hashing) and L2-normalises it. There
is no semantics: "car" and "vehicle" share no features. What it does
give is a vector lane that differs from FTS5 — trigrams still overlap
for misspellings and joined or split compounds ("maintenence",
"passcode" vs "pass code") where porter-stemmed keyword search finds
nothing — so golden questions can prove the vector lanes are wired in.

``blake2b`` rather than ``hash()`` so vectors are identical across
processes, Python builds and platforms.
"""

import hashlib
import math
import re
from collections.abc import Callable

from src.database import EMBEDDING_DIM

_WORD = re.compile(r"\w+")
_WORD_WEIGHT = 1.0
_TRIGRAM_WEIGHT = 0.5


def _features(text: str):
    for word in _WORD.findall(text.lower()):
        yield "w:" + word, _WORD_WEIGHT
        padded = f"#{word}#"
        for i in range(len(padded) - 2):
            yield "t:" + padded[i : i + 3], _TRIGRAM_WEIGHT


def embed_text(text: str) -> list[float]:
    """Unit-norm hashed feature vector for ``text`` (zero vector if it
    has no word characters)."""
    vec = [0.0] * EMBEDDING_DIM
    for feature, weight in _features(text):
        h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
        vec[h % EMBEDDING_DIM] += weight if h >> 63 == 0 else -weight
    norm = math.sqrt(sum(x * x for x in vec))
    return vec if norm == 0 else [x / norm for x in vec]


class HashEmbedder:
    """``EmbeddingBackend`` implementation over ``embed_text``."""

    def wait_for_ready(self, timeout: int = 120) -> None:
        return None

    def embed(self, text: str) -> list[float]:
        return embed_text(text)

    def embed_batch(
        self,
        texts: list[str],
        *,
        on_batch_complete: Callable[[], None] | None = None,
    ) -> list[list[float]]:
        vectors = [embed_text(t) for t in texts]
        if on_batch_complete is not None:
            on_batch_complete()
        return vectors
