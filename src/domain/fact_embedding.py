"""The texts embedded for a fact, shared by every writer so vectors stay comparable."""

import json
from typing import Any

from .entities import FACT_SYSTEM_TAGS


def fact_embedding_texts(
    text: str, tags: list[str] | None, metadata: dict[str, Any] | None
) -> list[str]:
    """The three texts embedded for a fact, in order: text, tags, metadata (each falls back to text when empty)."""
    # System tags are added after the creation embedding; leaving them out keeps re-embedding equal to creation.
    kept = [t for t in (tags or []) if t not in FACT_SYSTEM_TAGS]
    tags_text = " ".join(kept) if kept else text
    metadata_text = json.dumps(metadata, ensure_ascii=False) if metadata else text
    return [text, tags_text, metadata_text]
