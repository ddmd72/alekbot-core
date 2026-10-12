"""
Recall@K and the recorded bars (USER_DRIVE_INDEXING_RFC §17.8). Pure.

A case is a hit when every expected path is among the paths of its top K cards; a case with more than
K expected paths is a hit when K of them are (RFC §17.8). A case may ask for a smaller K (`"k": 1`:
the file must come first), or for fewer hits (`"min_hits": 5`: five of a trip's twelve photos, when the
rest of its top K is the trip's folder and other right answers). A collapsed duplicate card covers its own
path and its `also_at` paths.

A G2 case (Slice 2+4, §24.11) is a hit when its paths are, and when every expected substring is in the window of
the card that holds its path: the best chunk and its neighbours, as Smart reads them (§10.2). Substrings are
compared after NFKC, casefold and whitespace collapsing, so a scan's thin spaces or a model's line breaks do not
decide a case. A case's labels (its goal, `ocr`) are scopes of their own next to its case type.

Bars pin what they were measured on: K, the tree, the gold files and the served bytes (by SHA-256), and each case's
outcome. A change to any of them gives no verdict until the bars are recorded again.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

OVERALL = "overall"
CASE_TYPES = frozenset({"exact", "partial", "translit", "misspelled", "kind", "date", "place", "folder",
                        "filter_only", "version_family", "semantic",
                        # G2: answer from content (§17.8, §24.11)
                        "clause", "amount", "invoice_number", "table", "two_column", "fact"})
_EPS = 1e-9
_SPACE = re.compile(r"\s+")

Substrings = Union[str, Mapping[str, str]]


@dataclass(frozen=True)
class CaseOutcome:
    case_id: str
    case: str
    hit: bool
    labels: Tuple[str, ...] = ()        # extra scopes: the goal ("G1", "G2") and "ocr"


def normalize_snippet(text: str) -> str:
    """NFKC (no-break and thin spaces become spaces), casefold, soft hyphens dropped, whitespace runs collapsed."""
    text = unicodedata.normalize("NFKC", text).replace("\u00ad", "").casefold()
    return _SPACE.sub(" ", text).strip()


def expected_substrings(expected_paths: Sequence[str], substring: Optional[Substrings]) -> Dict[str, str]:
    """{path: substring}: a plain string belongs to the case's first expected path."""
    if substring is None:
        return {}
    if isinstance(substring, str):
        return {expected_paths[0]: substring}
    return dict(substring)


def substring_hit(wanted: Mapping[str, str], windows: Mapping[str, str]) -> bool:
    """Every expected substring is in the window shown for its path (§10.2)."""
    return all(normalize_snippet(text) in normalize_snippet(windows.get(path, "")) for path, text in wanted.items())


def case_hit(expected: Sequence[str], covered: Set[str], k: int, min_hits: Optional[int] = None) -> bool:
    found = sum(1 for path in expected if path in covered)
    return found >= (min_hits if min_hits is not None else min(len(expected), k))


def recall(outcomes: Sequence[CaseOutcome]) -> Dict[str, Dict[str, float]]:
    """{scope: {"cases", "hits", "recall"}} for each case type and OVERALL."""
    groups: Dict[str, List[CaseOutcome]] = {OVERALL: list(outcomes)}
    for outcome in outcomes:
        for scope in (outcome.case, *outcome.labels):
            groups.setdefault(scope, []).append(outcome)
    table: Dict[str, Dict[str, float]] = {}
    for scope, members in sorted(groups.items()):
        hits = sum(1 for m in members if m.hit)
        table[scope] = {"cases": len(members), "hits": hits, "recall": hits / len(members) if members else 0.0}
    return table


def make_bars(table: Mapping[str, Mapping[str, float]], outcomes: Sequence[CaseOutcome], k: int,
              embedding_space: str, recorded_at: str, tree_sha256: str, gold_sha256: str,
              files_sha256: Optional[str] = None) -> Dict[str, Any]:
    bars: Dict[str, Any] = {
        "k": k,
        "embedding_space": embedding_space,
        "recorded_at": recorded_at,
        "tree_sha256": tree_sha256,
        "gold_sha256": gold_sha256,
        "recall": {scope: row["recall"] for scope, row in table.items()},
        "cases": {o.case_id: o.hit for o in sorted(outcomes, key=lambda o: o.case_id)},
    }
    if files_sha256 is not None:
        bars["files_sha256"] = files_sha256
    return bars


def bar_mismatch(bars: Mapping[str, Any], k: int, tree_sha256: str, gold_sha256: str,
                 case_ids: Sequence[str], files_sha256: Optional[str] = None) -> List[str]:
    """Why these bars cannot judge this run (empty: they can). Added and removed cases are named."""
    reasons: List[str] = []
    if bars.get("k") != k:
        reasons.append(f"K changed: bars {bars.get('k')}, now {k}")
    if bars.get("tree_sha256") != tree_sha256:
        reasons.append("tree.json changed since the bars were recorded")
    if files_sha256 is not None and bars.get("files_sha256") != files_sha256:
        reasons.append("the fixture files (or a content template) changed since the bars were recorded")
    if bars.get("gold_sha256") != gold_sha256:
        recorded = set(bars.get("cases", {}))
        now = set(case_ids)
        added, removed = sorted(now - recorded), sorted(recorded - now)
        reasons.append(f"the gold files changed since the bars were recorded (added {added}, removed {removed})")
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
