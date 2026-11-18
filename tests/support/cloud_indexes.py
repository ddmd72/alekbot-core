"""The index model of the cloud gate: `config/firestore.indexes.json` entries rendered for the Firestore test database and
turned into Admin API indexes (TEST_COVERAGE_SYSTEMIC_RFC §4 "Indexes"). Pure: no client, no network, tested offline.

The render keeps the `development_*` composites of one collection group and renames the group to `test_*` (the prefix of
`APP_ENV=test`); field overrides (single-field configs and exemptions) are not rendered. A signature is the scope plus
`(field, mode)` per field without `__name__` (the server appends `__name__` to every composite), where the mode is the
order, `CONTAINS` or `VECTOR`; it is what decides whether a wanted index already exists.
"""
import json
from pathlib import Path
from typing import Any, Collection, Dict, List, Optional, Tuple, Union

LIVE_PREFIX, TEST_PREFIX = "development_", "test_"


def rendered_group(group: str) -> Optional[str]:
    """The test database's name for a live `development_*` collection group; None for any other name."""
    return TEST_PREFIX + group[len(LIVE_PREFIX):] if group.startswith(LIVE_PREFIX) else None


def dimensions(vector_dimension: Union[None, int, Collection[int]]) -> Optional[Tuple[int, ...]]:
    """`vector_dimension` as a sorted tuple of dimensions (one int or several), or None."""
    if vector_dimension is None:
        return None
    return (vector_dimension,) if isinstance(vector_dimension, int) else tuple(sorted(set(vector_dimension)))


def render_indexes(group: str, indexes: Union[str, Path, List[Dict[str, Any]]],
                   vector_dimension: Union[None, int, Collection[int]] = None) -> List[Dict[str, Any]]:
    """The composites `indexes` (the file or its parsed list) declares on the live group `group`, renamed to its test
    group; with `vector_dimension` (one dimension or several), each vector index once per dimension (a kit's test vectors
    are short, and Firestore finds a vector only through an index of its own dimension). Empty for a group that is not
    `development_*` or has none."""
    target = rendered_group(group)
    raw = json.loads(Path(indexes).read_text()) if isinstance(indexes, (str, Path)) else indexes
    if target is None:
        return []
    dims = dimensions(vector_dimension)
    rendered = []
    for index in raw:
        if index.get("collectionGroup") != group or "fields" not in index:
            continue
        has_vector = any("vectorConfig" in f for f in index["fields"])
        for dim in (dims if dims and has_vector else (None,)):
            fields = [dict(f, vectorConfig=dict(f["vectorConfig"], dimension=dim)) if dim and "vectorConfig" in f else f
                      for f in index["fields"]]
            rendered.append(dict(index, collectionGroup=target, fields=fields))
    return rendered


def _mode(field: Dict[str, Any]) -> str:
    if "vectorConfig" in field:
        return "VECTOR"
    if "arrayConfig" in field:
        return "CONTAINS"
    return field.get("order", "ASCENDING")


def wanted_signature(index: Dict[str, Any]) -> Tuple[str, Tuple[Tuple[str, str], ...]]:
    return (index.get("queryScope", "COLLECTION"),
            tuple((f["fieldPath"], _mode(f)) for f in index["fields"] if f["fieldPath"] != "__name__"))


def existing_signature(index: Any) -> Tuple[str, Tuple[Tuple[str, str], ...]]:
    def mode(f: Any) -> str:
        if f.vector_config.dimension:
            return "VECTOR"
        if f.array_config:
            return "CONTAINS"
        return f.order.name

    from google.cloud.firestore_admin_v1 import Index     # imported here: the unit suite loads this module through kit_backend

    return (Index.QueryScope(index.query_scope).name,
            tuple((f.field_path, mode(f)) for f in index.fields if f.field_path != "__name__"))


def of_group(indexes: Any, group: str) -> List[Any]:
    """The Admin API indexes that belong to `group`: `list_indexes` under one collection group's parent answers with the
    indexes of every group of the database (seen on the test database 2026-11-18), so the caller filters by name."""
    return [i for i in indexes if i.name.split("/collectionGroups/", 1)[-1].split("/", 1)[0] == group]


def wanted_key(index: Dict[str, Any]) -> Tuple[Any, ...]:
    """`wanted_signature` plus the dimension of each vector field: two vector indexes that differ only in dimension are
    different indexes (what `ensure_indexes` compares)."""
    return wanted_signature(index), tuple(f["vectorConfig"]["dimension"] for f in index["fields"] if "vectorConfig" in f)


def existing_key(index: Any) -> Tuple[Any, ...]:
    return existing_signature(index), tuple(f.vector_config.dimension for f in index.fields if f.vector_config.dimension)


def admin_index(index: Dict[str, Any]) -> Any:
    """The Admin API `Index` of one entry in `config/firestore.indexes.json` shape (`__name__` passed as declared)."""
    from google.cloud.firestore_admin_v1 import Index

    field_cls = Index.IndexField
    vector = field_cls.VectorConfig

    def field(f: Dict[str, Any]) -> Any:
        if "vectorConfig" in f:
            return field_cls(field_path=f["fieldPath"], vector_config=vector(dimension=f["vectorConfig"]["dimension"],
                                                                             flat=vector.FlatIndex()))
        if "arrayConfig" in f:
            return field_cls(field_path=f["fieldPath"], array_config=field_cls.ArrayConfig[f["arrayConfig"]])
        return field_cls(field_path=f["fieldPath"], order=field_cls.Order[f.get("order", "ASCENDING")])

    return Index(query_scope=Index.QueryScope[index.get("queryScope", "COLLECTION")],
                 fields=[field(f) for f in index["fields"]])
