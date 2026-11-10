"""The `when:` line of a fact as the memory search renders it (fact profile RFC §5.4), shared by the memory agent and
the connector. Pure: the stored time points are already written at their own precision."""
from typing import List, Optional

from .entities import FactEntity


def when_line(fact: FactEntity) -> Optional[str]:
    """`when: <point>; <point>`: a point is its stored `from`, plus `–<to>` when `to` differs. None without points."""
    points: List[str] = []
    for point in fact.time_points or []:
        start = str(point.get("from") or "").strip()
        if not start:
            continue
        end = str(point.get("to") or "").strip()
        points.append(f"{start}–{end}" if end and end != start else start)
    return "when: " + "; ".join(points) if points else None
