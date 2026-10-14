"""
LexicalEmbeddings — an offline, deterministic stand-in for the embedding model (USER_DRIVE_INDEXING_RFC §25.7).

The Search v2 eval runs with no model call (F-22): the enrichment comes from `enrichment.json`, and the vectors from
this class. Each text is the signed hashing-trick sum of its words and their character trigrams (casefolded, accents
stripped), L2-normalised, in `DIMENSIONS` dimensions. Texts that share words or word parts are close; texts in two
languages that share no spelling are not, so the meaning streams (card, chunk and hint kNN) measure lexical overlap,
not meaning. What that means for the bars: the exact and automatic tiers do not read vectors (keys decide them), so
their numbers hold; the Related tier and the cross-language cases reached only by meaning are a floor here, and the
owner's run with the real model (`--embeddings gemini`) is the one that judges them.

Passage keywords meet grounding rule 4 at index time (§25.4.2): a keyword set whose vector is far from its chunk's is
dropped. That is a meaning check, which lexical vectors cannot make: a list of keywords shares few trigrams with the
passage it describes. `AnchoredLexicalEmbeddings` therefore embeds a recorded keyword set as the passage it was
written for (the remembered text holding the items' evidence quotes), so offline every grounded set passes rule 4 —
the By-translation tier is measured as if the check kept every set (an upper bound), and the owner's real-model run
measures it with the check live.
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

DIMENSIONS = 256
EMBEDDING_SPACE = f"lexical-trigram/{DIMENSIONS}"
_WORD = re.compile(r"\w+", re.UNICODE)


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _features(text: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for word in _WORD.findall(_fold(text)):
        out["w:" + word] = out.get("w:" + word, 0.0) + 1.0
        padded = f"<{word}>"
        for i in range(len(padded) - 2):
            gram = "t:" + padded[i:i + 3]
            out[gram] = out.get(gram, 0.0) + 0.5
    return out


def embed(text: str) -> List[float]:
    vector = [0.0] * DIMENSIONS
    for feature, weight in _features(text).items():
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        slot = int.from_bytes(digest[:4], "big") % DIMENSIONS
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[slot] += sign * weight
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        vector[0] = 1.0   # an empty text still has a direction (the store refuses a zero vector)
        return vector
    return [x / norm for x in vector]


class LexicalEmbeddings:
    """The EmbeddingService surface the indexer and the search call. `calls` counts texts embedded."""

    EMBEDDING_SPACE = EMBEDDING_SPACE

    def __init__(self) -> None:
        self.calls = 0

    async def get_embedding(self, text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> List[float]:
        self.calls += 1
        return embed(text)

    async def get_embeddings_batch(self, texts: List[str], task_type: str = "RETRIEVAL_DOCUMENT") -> List[List[float]]:
        self.calls += len(texts)
        return [embed(t) for t in texts]


def _norm(text: str) -> str:
    return " ".join(_fold(text).split())


class AnchoredLexicalEmbeddings(LexicalEmbeddings):
    """LexicalEmbeddings whose recorded passage-keyword sets are embedded as their passage (module docstring).
    `evidence_of` maps a recorded item's text to its evidence quotes. Only document texts are anchored: a query
    (`RETRIEVAL_QUERY`) always embeds as itself, so a query that happens to equal a keyword is never handed its
    passage's vector."""

    def __init__(self, evidence_of: Mapping[str, Sequence[str]]) -> None:
        super().__init__()
        self._evidence_of = {text: [_norm(q) for q in quotes if q.strip()] for text, quotes in evidence_of.items()}
        self._seen: List[Tuple[str, str]] = []    # (text, normalised) of every document text embedded so far
        self.anchored = 0

    def add_evidence(self, text: str, quote: str) -> None:
        """Register one more recorded item (the scale runs' synthetic passages register theirs as they answer)."""
        if quote.strip():
            self._evidence_of.setdefault(text, []).append(_norm(quote))

    def _anchor(self, text: str) -> Optional[str]:
        """The latest document text that holds one evidence quote of every line (a keyword recorded with several
        passages has several quotes)."""
        lines = [line for line in text.split("\n") if line.strip()]
        if not lines or not all(self._evidence_of.get(line) for line in lines):
            return None
        for seen, folded in reversed(self._seen):
            if all(any(q in folded for q in self._evidence_of[line]) for line in lines):
                return seen
        return None

    def _one(self, text: str, task_type: str) -> List[float]:
        if task_type == "RETRIEVAL_QUERY":
            return embed(text)
        anchor = self._anchor(text)
        if anchor is not None:
            self.anchored += 1
            return embed(anchor)
        self._seen.append((text, _norm(text)))   # a chunk or a card: a later keyword set may be anchored to it
        return embed(text)

    async def get_embedding(self, text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> List[float]:
        self.calls += 1
        return self._one(text, task_type)

    async def get_embeddings_batch(self, texts: List[str], task_type: str = "RETRIEVAL_DOCUMENT") -> List[List[float]]:
        self.calls += len(texts)
        return [self._one(t, task_type) for t in texts]
