"""
Search v2 eval metrics (USER_DRIVE_INDEXING_RFC §25.7). Pure: rows in, family table and verdict out.

A v2 row says where each file landed: `tiers` maps a path to its tier (`about`, `mentioned`, `translated`,
`labelled`, `related`; every member of a tier, shown or grouped), `similar` lists the Related lines that are similar
names, `absent` the words no file holds (D29), and `chars` the rendered result's length. A v1 row (the tag's
`drive-search-v1` run on the same fixture, `baseline_v1.json`) has only its top-K cards: v1 had no tiers, so every
card it returned was presented as a match and is scored as one (`V1_TIER`).

Pass rules per family (§25.7's table):
- `exact`: every expected file in About or Mentioned in;
- `semantic` (cross-language, translation, synonyms, related): every expected file in any tier, Related included;
- `class`: every expected file in Labelled or an exact tier (recall is pooled over files);
- `incident`: every expected file in a tier other than Related;
- `misspelling`: the misspelled word named absent (the D29 line), or the expected file shown at all (under "similar
  name" or better);
- `negative`: About, Mentioned, By translation and Labelled all empty.
A case may carry `known_hard` (a reason): it is run and reported (`known_hard` in the table: id, reason, whether it
passed) but guards nothing — no pass rate, recall, junk, book or size line counts it — for a gap the eval found and the
owner has not decided yet, kept visible instead of dropped (G1 has the same key, runner.py).
A case may carry `requires_embeddings: "semantic"` (a meaning-only case that lexical vectors cannot represent): under
`--embeddings lexical` it is reported as skipped (`split_by_embeddings`) and counted in no family rate; under `gemini`
it counts like any other.
A G1/G2 case may carry `accepted_loss` (a reason): the owner accepted that v2 no longer finds it (e.g. D29 strict AND).
`g1_table` lists it under `accepted_losses` and leaves it out of the "v1-passing cases lost" bar.
A case may add `expect_inaccessible: true` (§26.2): every expected file must also be a card whose `index:` line says the bot
cannot open it (the row's `inaccessible` paths), on top of the family's tier rule.
A case may add `expect_accuracy: {path: true|false}` (§26.4): the card shown for that path does (true) or does not
(false: a photograph) carry the `card accuracy: N%` line (the row's `accuracy` map, None = no line), on top of the
family's tier rule. A path the search did not show as a card fails a `true`.
Junk: in an exact family case, the About/Mentioned files that are neither expected nor `acceptable_paths`; on every
positive case, the same for By translation and Labelled. The book distractor counts on cases of two or more concepts
whose expected and acceptable files do not include it.
G1/G2 (the v1 gold) are scored by their case type's family: listings (filter-only) by v1's top-K rule, misspellings
by the misspelling rule, translit and semantic by the semantic rule, everything else by the exact rule, and G2
content cases by the class rule (a content answer needs the right file in a matched tier; its substring is reported,
since an About card carries no passage).
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from src.domain.drive_index_params import K as LISTING_K

from .metrics import case_hit

FAMILIES = ("exact", "semantic", "class", "incident", "misspelling", "negative")
POSITIVE_FAMILIES = frozenset({"exact", "semantic", "class", "incident"})
TIERS = ("about", "mentioned", "translated", "labelled", "related")
EXACT_TIERS = frozenset({"about", "mentioned"})
AUTOMATIC_TIERS = frozenset({"translated", "labelled"})
MATCH_TIERS = EXACT_TIERS | AUTOMATIC_TIERS
EXPECT_TIERS = frozenset(TIERS) | {"exact", "automatic", "any", "none"}
V1_TIER = "v1"            # a v1 card: presented as a match, no tier
LISTING = "listing"       # a filter-only listing's card (no tier in v2 either)
BOOK = "Books/A Short History of Bridges.docx"
SEMANTIC = "semantic"
G1_FAMILY = {"misspelled": "misspelling", "translit": "semantic", "semantic": "semantic",
             "clause": "class", "amount": "class", "invoice_number": "class", "table": "class", "two_column": "class",
             "fact": "class"}


class GoldError(ValueError):
    """A malformed v2 gold case: it would score something other than what it says."""


def validate_gold_v2(gold: Sequence[Mapping[str, Any]], paths: Iterable[str]) -> None:
    known = set(paths)
    problems: List[str] = []
    ids = [c.get("id") for c in gold]
    problems += [f"duplicate id {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    for case in gold:
        cid, family = case.get("id"), case.get("family")
        if family not in FAMILIES:
            problems.append(f"{cid}: unknown family {family!r}")
        if case.get("expect_tier") not in EXPECT_TIERS:
            problems.append(f"{cid}: expect_tier must be one of {sorted(EXPECT_TIERS)}")
        expected = case.get("expected_paths") or []
        if family in POSITIVE_FAMILIES and not expected:
            problems.append(f"{cid}: needs expected_paths (family {family})")
        if "expect_inaccessible" in case and (case["expect_inaccessible"] is not True or family not in POSITIVE_FAMILIES):
            problems.append(f"{cid}: expect_inaccessible must be true, on a positive case")
        if "expect_accuracy" in case:
            wanted = case["expect_accuracy"]
            if not (isinstance(wanted, dict) and wanted and all(isinstance(v, bool) for v in wanted.values())
                    and family in POSITIVE_FAMILIES):
                problems.append(f"{cid}: expect_accuracy must be a non-empty {{path: bool}} map, on a positive case")
            problems += [f"{cid}: expect_accuracy names unknown path {p}" for p in wanted if p not in known] \
                if isinstance(wanted, dict) else []
        if "known_hard" in case and not (isinstance(case["known_hard"], str) and case["known_hard"].strip()):
            problems.append(f"{cid}: known_hard must be a non-empty reason")
        if "requires_embeddings" in case and case["requires_embeddings"] != SEMANTIC:
            problems.append(f"{cid}: requires_embeddings must be {SEMANTIC!r}")
        if "requires_enrichment" in case and case["requires_enrichment"] is not True:
            problems.append(f"{cid}: requires_enrichment must be true")
        if family == "negative" and expected:
            problems.append(f"{cid}: a negative case expects no file")
        if family == "misspelling" and not (expected or case.get("expect_absent")):
            problems.append(f"{cid}: a misspelling case needs expected_paths or expect_absent")
        for p in list(expected) + list(case.get("acceptable_paths") or []):
            if p not in known:
                problems.append(f"{cid}: {p} is not in the tree")
        if not str(case.get("query", "")).strip():
            problems.append(f"{cid}: a v2 case needs words (filter-only listings are G1's)")
    if problems:
        raise GoldError("malformed v2 gold: " + "; ".join(problems))


def split_by_embeddings(gold: Sequence[Mapping[str, Any]], kind: str
                        ) -> "tuple[List[Mapping[str, Any]], List[Mapping[str, Any]]]":
    """(cases to score, cases skipped): a `requires_embeddings: semantic` case is skipped under lexical vectors."""
    if kind == "gemini":
        return list(gold), []
    kept = [c for c in gold if c.get("requires_embeddings") != SEMANTIC]
    return kept, [c for c in gold if c.get("requires_embeddings") == SEMANTIC]


def g1_family(case: Mapping[str, Any]) -> str:
    return G1_FAMILY.get(str(case.get("case")), "exact")


# -- one case ------------------------------------------------------------------------------------------------------------

def _in(tiers: Mapping[str, str], path: str, allowed: Iterable[str]) -> bool:
    return tiers.get(path) in set(allowed)


def _allowed(family: str, v1: bool) -> Set[str]:
    if v1:
        return {V1_TIER, LISTING}
    return {"exact": EXACT_TIERS, "semantic": set(TIERS), "class": MATCH_TIERS,
            "incident": MATCH_TIERS, "misspelling": set(TIERS)}.get(family, set())


def case_pass(case: Mapping[str, Any], row: Mapping[str, Any], *, family: Optional[str] = None,
              v1: bool = False) -> bool:
    """Whether a row meets its family's pass rule (module docstring). `family` overrides the case's (G1/G2)."""
    if row.get("error"):
        return False
    fam = family or case["family"]
    tiers: Mapping[str, str] = row.get("tiers") or {}
    expected = list(case.get("expected_paths") or [])
    if row.get("listing"):   # v1's rule: the expected files among the first K cards (K, min_hits per case)
        k = int(case.get("k", LISTING_K))
        cards = list(row.get("cards") or [[p] for p in tiers])[:k]
        covered = {p for card in cards for p in card}
        return case_hit(expected, covered, k, case.get("min_hits"))
    if fam == "negative":
        return not any(t in (MATCH_TIERS | {V1_TIER}) for t in tiers.values())
    if fam == "misspelling":
        absent = {w.casefold() for w in row.get("absent") or ()}
        if any(w.casefold() in absent for w in case.get("expect_absent") or ()):
            return True
        if not expected:
            return not any(t in (MATCH_TIERS | {V1_TIER}) for t in tiers.values())
        return any(_in(tiers, p, _allowed(fam, v1)) for p in expected)
    if case.get("expect_inaccessible") and not v1:
        shown = set(row.get("inaccessible") or ())
        if not all(p in shown for p in expected):
            return False
    if case.get("expect_accuracy") and not v1:
        shown = row.get("accuracy") or {}
        for path, has_line in case["expect_accuracy"].items():
            if has_line != (shown.get(path) is not None):
                return False
    return all(_in(tiers, p, _allowed(fam, v1)) for p in expected)


def found_files(case: Mapping[str, Any], row: Mapping[str, Any], *, family: Optional[str] = None,
                v1: bool = False) -> int:
    fam = family or case["family"]
    tiers = row.get("tiers") or {}
    return sum(1 for p in case.get("expected_paths") or [] if _in(tiers, p, _allowed(fam, v1)))


def junk(case: Mapping[str, Any], row: Mapping[str, Any], tiers_of: Iterable[str]) -> int:
    allowed = set(case.get("expected_paths") or []) | set(case.get("acceptable_paths") or [])
    wanted = set(tiers_of)
    return sum(1 for path, tier in (row.get("tiers") or {}).items() if tier in wanted and path not in allowed)


def book_in_exact(case: Mapping[str, Any], row: Mapping[str, Any], *, v1: bool = False) -> bool:
    """The long-document distractor shown as an exact match on a case of two or more concepts that is not about it."""
    if len(row.get("concepts") or ()) < 2 and not v1:
        return False
    if v1 and len(str(case.get("query", "")).split()) < 2:
        return False
    if BOOK in set(case.get("expected_paths") or []) | set(case.get("acceptable_paths") or []):
        return False
    tier = (row.get("tiers") or {}).get(BOOK)
    return tier in ({V1_TIER} if v1 else EXACT_TIERS)


# -- the table -----------------------------------------------------------------------------------------------------------

def _rate(hits: float, n: float) -> Optional[float]:
    return hits / n if n else None


def family_table(gold: Sequence[Mapping[str, Any]], rows: Mapping[str, Mapping[str, Any]], *,
                 v1: bool = False) -> Dict[str, Any]:
    """Per family: cases, passes, the pass rate and (class) the pooled file recall; then the junk, book, related,
    and size lines of §25.7."""
    out: Dict[str, Any] = {"families": {}}
    out["known_hard"] = {c["id"]: {"reason": c["known_hard"], "passed": case_pass(c, rows[c["id"]], v1=v1)}
                         for c in gold if c.get("known_hard") and c["id"] in rows}
    gold = [c for c in gold if not c.get("known_hard")]   # reported above, never counted below
    exact_junk: List[int] = []
    auto_junk: List[int] = []
    book_hits: List[str] = []
    related_neg: List[int] = []
    chars: List[int] = []
    for fam in FAMILIES:
        members = [c for c in gold if c["family"] == fam and c["id"] in rows]
        if not members:
            continue
        passed = [c["id"] for c in members if case_pass(c, rows[c["id"]], v1=v1)]
        line: Dict[str, Any] = {"cases": len(members), "passed": len(passed),
                                "rate": _rate(len(passed), len(members)),
                                "failed": sorted(c["id"] for c in members if c["id"] not in passed)}
        if fam in POSITIVE_FAMILIES:
            files = sum(len(c.get("expected_paths") or []) for c in members)
            found = sum(found_files(c, rows[c["id"]], v1=v1) for c in members)
            line.update(files=files, files_found=found, file_recall=_rate(found, files))
        out["families"][fam] = line
    for case in gold:
        row = rows.get(case["id"])
        if row is None:
            continue
        if row.get("chars") is not None:
            chars.append(int(row["chars"]))
        fam = case["family"]
        if fam == "exact":
            exact_junk.append(junk(case, row, {V1_TIER} if v1 else EXACT_TIERS))
        if fam in POSITIVE_FAMILIES and not v1:
            auto_junk.append(junk(case, row, AUTOMATIC_TIERS))
        if fam == "negative" and not v1:
            related_neg.append(sum(1 for t in (row.get("tiers") or {}).values() if t == "related"))
        if book_in_exact(case, row, v1=v1):
            book_hits.append(case["id"])
    out["exact_junk_per_case"] = statistics.mean(exact_junk) if exact_junk else None
    out["automatic_junk_per_case"] = statistics.mean(auto_junk) if auto_junk else None
    out["related_per_negative"] = statistics.mean(related_neg) if related_neg else None
    out["book_in_exact"] = book_hits
    out["chars_median"] = statistics.median(chars) if chars else None
    out["chars_max"] = max(chars) if chars else None
    return out


def g1_table(gold: Sequence[Mapping[str, Any]], rows: Mapping[str, Mapping[str, Any]],
             v1_passed: Mapping[str, bool]) -> Dict[str, Any]:
    """The v1 gold under v2 by its family's rule: every case v1 passed must pass (§25.7's last bar)."""
    lost = []
    accepted = []
    passed = []
    substring_misses = []
    for case in gold:
        row = rows.get(case["id"])
        if row is None:
            continue
        ok = case_pass(case, row, family=g1_family(case))
        if ok:
            passed.append(case["id"])
        if v1_passed.get(case["id"]) and not ok:
            (accepted if case.get("accepted_loss") else lost).append(case["id"])
        if row.get("substring_hit") is False:
            substring_misses.append(case["id"])
    guarded = [c["id"] for c in gold if v1_passed.get(c["id"])]
    return {"cases": len(gold), "passed": len(passed), "v1_passed": len(guarded), "lost": sorted(lost),
            "accepted_losses": {c: next(g["accepted_loss"] for g in gold if g["id"] == c) for c in sorted(accepted)},
            "substring_not_in_card": sorted(substring_misses)}


def judge(table: Mapping[str, Any], g1: Optional[Mapping[str, Any]], bars: Mapping[str, Any]) -> List[str]:
    """Every bar the run misses (empty: pass). Bars are the pre-registered numbers of bars_v2.json."""
    out: List[str] = []
    fams = table.get("families", {})

    def below(name: str, value: Optional[float], bar: float) -> None:
        if value is None or value < bar - 1e-9:
            out.append(f"{name}: {value} < {bar}")

    def above(name: str, value: Optional[float], bar: float) -> None:
        if value is not None and value > bar + 1e-9:
            out.append(f"{name}: {value} > {bar}")

    for fam, bar in bars.get("pass_rate", {}).items():
        if fam in fams:
            below(f"{fam} pass rate", fams[fam]["rate"], float(bar))
    for fam, bar in bars.get("file_recall", {}).items():
        if fam in fams:
            below(f"{fam} file recall", fams[fam].get("file_recall"), float(bar))
    above("exact junk per case", table.get("exact_junk_per_case"), float(bars["exact_junk_per_case_max"]))
    above("automatic junk per case", table.get("automatic_junk_per_case"),
          float(bars["automatic_junk_per_case_max"]))
    above("related lines per negative", table.get("related_per_negative"), float(bars["related_per_negative_max"]))
    if len(table.get("book_in_exact") or ()) > int(bars["book_in_exact_max"]):
        out.append(f"book distractor in an exact tier: {table['book_in_exact']}")
    above("median characters", table.get("chars_median"), float(bars["chars_median_max"]))
    above("max characters", table.get("chars_max"), float(bars["chars_max"]))
    if g1 is not None and g1.get("lost"):
        out.append(f"v1-passing G1/G2 cases lost: {g1['lost']}")
    return out
