"""Whether a JSON body carries an ALLOWED gap of a provider contract (`scripts/contracts/extract_subset.Allowed`), for
the meta-tests that refuse a gap nothing exercises (P17).

A body path as the extractor writes it (`a.b[]`, "" = the body); a header gap is its test's own check. The match is the
gap's own kind: a value (an enum literal, or the `type` of a union variant) at the path, the JSON value null at the
path, a field of the object at the path; `variant` keeps it to the typed object of that `type` (the object at the path
for a field, the object holding the node for a null), as the extractor applies it.
"""
from __future__ import annotations

from typing import Any


def values_at(node: Any, path: str) -> list[Any]:
    """The values at `a.b[]` in a body (`[]` walks an array's items; "" is the body itself); missing parts yield
    nothing."""
    nodes = [node]
    for step in path.split(".") if path else []:
        many = step.endswith("[]")
        key = step[:-2] if many else step
        nodes = [x for n in nodes if isinstance(n, dict) and key in n
                 for x in (n[key] if many and isinstance(n[key], list) else [n[key]])]
    return nodes


def _typed(node: Any, variant: str | None) -> bool:
    return isinstance(node, dict) and (variant is None or node.get("type") == variant)


def carries(allowed: Any, body: Any) -> bool:
    """True when `body` carries the gap `allowed` where the contract puts it. A kind this helper cannot judge raises:
    read as False it would fail with a wrong message, read as True it would hide a dead entry."""
    if allowed.path.startswith("header:") or allowed.path == "query":
        raise ValueError(f"{allowed.path!r}: not a body path; the test checks it itself")
    if allowed.null:
        parent, _, leaf = allowed.path.rpartition(".")
        if leaf.endswith("[]"):
            raise NotImplementedError(f"a null array item at {allowed.path!r}")
        return any(_typed(h, allowed.variant) and leaf in h and h[leaf] is None for h in values_at(body, parent))
    if allowed.value is not None:
        if allowed.variant is not None:
            raise NotImplementedError(f"a value narrowed to a variant at {allowed.path!r}")
        return any(v == allowed.value or (isinstance(v, dict) and v.get("type") == allowed.value)
                   for v in values_at(body, allowed.path))
    return any(_typed(o, allowed.variant) and allowed.field in o for o in values_at(body, allowed.path))
