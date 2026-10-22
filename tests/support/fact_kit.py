"""Seeding helpers shared by the fact store kit (`tests/unit/adapters/test_fact_*_kit.py`).

`make_fact` builds a FactEntity; `put_raw` writes a raw document straight into the fake, for the documents no writer of
this code base produces any more (legacy `owner_id`, a missing field, a string date, an unknown enum value).
"""
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Optional

from google.cloud.firestore_v1.vector import Vector

from src.domain.entities import FactDomain, FactEntity, FactType
from tests.support.fact_store import FactStore, embed

ACC = "acc-1"
OTHER = "acc-2"
USER = "usr-1"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
DROP = object()     # as an override in put_raw: the field is not stored

_RANKS = {"critical": 1, "high": 2, "medium": 3, "low": 4, "archival": 5}


def make_fact(text: str, /, *, account: str = ACC, age_days: int = 0, **over: Any) -> FactEntity:
    """A current fact of `account`; `age_days` orders created_at; the three vectors come from `embed` of the text."""
    when = T0 + timedelta(days=age_days)
    fields: Dict[str, Any] = dict(
        id=f"f-{text}", account_id=account, created_by_user_id=USER, lineage_id=f"lin-{text}", text=text,
        type=FactType.EVENT, domain=FactDomain.HEALTH, created_at=when, valid_from=when, last_updated=when,
    )
    fields.update(over)
    for name, source in (("vector", text), ("tags_vector", "t " + text), ("metadata_vector", "m " + text)):
        fields.setdefault(name, embed(source))
    return FactEntity(**fields)


def put_raw(store: FactStore, doc_id: str, text: str = "raw", *, entity: Optional[Dict[str, Any]] = None,
            **over: Any) -> str:
    """Store the document of `make_fact(text, **entity)` under `doc_id`, with `over` applied on top of the raw fields
    (`DROP` removes a field). Enums are stored as their values, vectors as `Vector`, as the repository stores them."""
    fact = make_fact(text, **{"id": doc_id, **(entity or {})})
    doc: Dict[str, Any] = {}
    for key, value in fact.model_dump().items():
        if isinstance(value, Enum):
            value = value.value
        doc[key] = Vector(value) if key in ("vector", "tags_vector", "metadata_vector") else value
    doc["context_priority_rank"] = _RANKS[doc["context_priority"]]
    for key, value in over.items():
        if value is DROP:
            doc.pop(key, None)
        else:
            doc[key] = value
    store.docs[doc_id] = doc
    return doc_id


def as_legacy(**over: Any) -> Dict[str, Any]:
    """Overrides that turn a raw document into a pre-migration one: `owner_id` instead of the two new fields."""
    return {"account_id": DROP, "created_by_user_id": DROP, "visibility": DROP, "owner_id": ACC, **over}
