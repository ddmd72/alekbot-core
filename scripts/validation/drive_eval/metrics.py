"""
Recall@K and the recorded bars (USER_DRIVE_INDEXING_RFC §17.8). Pure.

A case is a hit when every expected path is among the paths of its top K cards; a case with more than
K expected paths is a hit when K of them are (plan Task 0). A case may ask for a smaller K (`"k": 1`:
the file must come first). A collapsed duplicate card covers its own path and its `also_at` paths.

Bars pin what they were measured on: K, the tree and the gold file (by SHA-256), and each case's
outcome. A change to any of them gives no verdict until the bars are recorded again.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Sequence, Set

OVERALL = "overall"
CASE_TYPES = frozenset({"exact", "partial", "translit", "misspelled", "kind", "date", "place", "folder",
                        "filter_only", "version_family"})
_EPS = 1e-9


@dataclass(frozen=True)
class CaseOutcome:
    case_id: str
    case: str
    hit: bool


def case_hit(expected: Sequence[str], covered: Set[str], k: int) -> bool:
    found = sum(1 for path in expected if path in covered)
    return found >= min(len(expected), k)


def recall(outcomes: Sequence[CaseOutcome]) -> Dict[str, Dict[str, float]]:
    """{scope: {"cases", "hits", "recall"}} for each case type and OVERALL."""
    groups: Dict[str, List[CaseOutcome]] = {OVERALL: list(outcomes)}
    for outcome in outcomes:
        groups.setdefault(outcome.case, []).append(outcome)
    table: Dict[str, Dict[str, float]] = {}
    for scope, members in sorted(groups.items()):
        hits = sum(1 for m in members if m.hit)
        table[scope] = {"cases": len(members), "hits": hits, "recall": hits / len(members) if members else 0.0}
    return table


def make_bars(table: Mapping[str, Mapping[str, float]], outcomes: Sequence[CaseOutcome], k: int,
              embedding_space: str, recorded_at: str, tree_sha256: str, gold_sha256: str) -> Dict[str, Any]:
    return {
        "k": k,
        "embedding_space": embedding_space,
        "recorded_at": recorded_at,
        "tree_sha256": tree_sha256,
        "gold_sha256": gold_sha256,
        "recall": {scope: row["recall"] for scope, row in table.items()},
        "cases": {o.case_id: o.hit for o in sorted(outcomes, key=lambda o: o.case_id)},
    }


def bar_mismatch(bars: Mapping[str, Any], k: int, tree_sha256: str, gold_sha256: str,
                 case_ids: Sequence[str]) -> List[str]:
    """Why these bars cannot judge this run (empty: they can). Added and removed cases are named."""
    reasons: List[str] = []
    if bars.get("k") != k:
        reasons.append(f"K changed: bars {bars.get('k')}, now {k}")
    if bars.get("tree_sha256") != tree_sha256:
        reasons.append("tree.json changed since the bars were recorded")
    if bars.get("gold_sha256") != gold_sha256:
        recorded = set(bars.get("cases", {}))
        now = set(case_ids)
        added, removed = sorted(now - recorded), sorted(recorded - now)
        reasons.append(f"gold_g1.json changed since the bars were recorded (added {added}, removed {removed})")
    recorded_recall = bars.get("recall")
    if not isinstance(recorded_recall, Mapping) or OVERALL not in recorded_recall:
        reasons.append(f"the bars have no '{OVERALL}' recall (bars file edited or merged by hand?)")
    return reasons


def regressions(table: Mapping[str, Mapping[str, float]], bars: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Scopes whose recall fell below the recorded bar."""
    out: List[Dict[str, Any]] = []
    for scope, bar in sorted(bars.get("recall", {}).items()):
        now = table.get(scope, {}).get("recall", 0.0)
        if now < bar - _EPS:
            out.append({"scope": scope, "bar": bar, "now": now})
    return out


def lost_cases(outcomes: Sequence[CaseOutcome], bars: Mapping[str, Any]) -> List[str]:
    """Cases that hit when the bars were recorded and miss now."""
    hit_then = {case_id for case_id, hit in bars.get("cases", {}).items() if hit}
    return sorted(o.case_id for o in outcomes if o.case_id in hit_then and not o.hit)
