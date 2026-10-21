"""The texts embedded for a fact, shared by every writer so vectors stay comparable."""

import json
from typing import Any


# Added to a stored fact after its creation embedding; leaving them out keeps re-embedding equal to creation.
_SYSTEM_TAGS = frozenset({"consolidated", "anchor"})


def fact_embedding_texts(
    text: str, tags: list[str] | None, metadata: dict[str, Any] | None
) -> list[str]:
    """The three texts embedded for a fact, in order: text, tags, metadata (each falls back to text when empty)."""
    kept = [t for t in (tags or []) if t not in _SYSTEM_TAGS]
    tags_text = " ".join(kept) if kept else text
    metadata_text = json.dumps(metadata, ensure_ascii=False) if metadata else text
    return [text, tags_text, metadata_text]
