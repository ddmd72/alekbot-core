"""
Scale runs and the floor calibration procedure (USER_DRIVE_INDEXING_RFC §25.7, OQ-V6). Offline: the synthetic corpora
of scale_corpus.py on the in-memory repository, the real tick and the real search, lexical vectors, an in-process
text parser and the synthetic enrichment. No model call, no Firestore.

    venv/bin/python -m scripts.validation.drive_eval.scale_calibrate --sizes 200,1000,3000 [--report PATH]

What it measures per corpus size: index time and ticks, records and chunks; per search the in-process latency
(p50/p95; the in-memory store, so a lower bound of the live one, which the §25.7 gate of p95 ≤ 3 s at 3 000 files
judges on Firestore) and the store reads (documents returned by repository reads, the proxy of Firestore reads); the
per-source pool statistics (how often card, chunk and hint kNN abstain); and the calibration below.

The procedure (the same on the live scale test, with real vectors):
1. Run the negatives (held-out topics no file holds) and the near-misses (a topic's words misspelled, so no key
   matches) through the search, keeping every file's meaning score `z` (TierCapture).
2. For each candidate floor t (0.5 to 6.0 by 0.25): related lines per negative = the mean of min(RELATED_MAX, files
   with z ≥ t); near-miss recall = the share of near-misses whose top RELATED_MAX files by z (z ≥ t) hold a file of
   their topic.
3. `Z_RELATED` for the band is the smallest t with related lines per negative ≤ 1.0; it is accepted when the
   near-miss recall at t is ≥ 0.7, else the band is reported as "no floor meets both" (the floor stays as is).
4. `COMMON_SHARE`: count the negatives a common-word allowance turned into a match (must be 0), over the shares
   0.2 / 0.3 / 0.4 of the matched positives' concepts.
Offline, lexical vectors make these numbers a rehearsal of the method, not the production floors: the owner's live
scale run (real vectors, Firestore) sets the values in drive_index_params.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_extraction import PARSER, ExtractionOutcome, ExtractionRequest, ExtractionStatus
from src.domain.drive_index_params import RELATED_MAX, Z_RELATED
from src.domain.drive_search import DriveSearchRequest
from src.domain.drive_tiers import size_band
from src.ports.content_extractor_port import ContentExtractorPort

from .fixture_drive import FixtureDrive
from .lexical_embeddings import EMBEDDING_SPACE, AnchoredLexicalEmbeddings
from .eval_connection import EvalDriveConnections, eval_connection
from .runner import EVAL_USER, EvalClock, index_fixture
from .scale_corpus import TOPICS, Corpus, SyntheticPorts, make_corpus
from .search_v2_eval import TierCapture

_REPO = Path(__file__).resolve().parents[3]
FIXTURES = _REPO / "tests" / "fixtures" / "drive_eval"
GRID = [0.5 + 0.25 * i for i in range(23)]
NEG_RELATED_MAX = 1.0
NEAR_RECALL_MIN = 0.7
_READS = ("find_by_about_keys", "find_expiring", "find_chunks_by_word_keys", "get_chunk_keys", "find_hints_by_keys",
          "get_hint_keys", "find_nearest_hints", "find_nearest_cards", "find_nearest_chunks", "get_hints",
          "get_records_lean", "get_records", "get_chunks", "find_by_name_keys", "filter_records",
          "count_by_job_state", "children")


class TextParser(ContentExtractorPort):
    """UTF-8 text files read in process (the scale runs measure the index and the search, not the parser)."""

    @property
    def extractor_id(self) -> str:
        return PARSER

    @property
    def version(self) -> int:
        return 2

    async def extract(self, request: ExtractionRequest) -> ExtractionOutcome:
        text = Path(request.path).read_bytes().decode("utf-8", errors="replace")
        status = ExtractionStatus.TEXT if text.strip() else ExtractionStatus.NO_TEXT
        return ExtractionOutcome(status=status, text=text, extracted_by=f"{PARSER}@2")


class CountingRepo(InMemoryDriveIndexRepository):
    """Counts the documents each repository read returns (the proxy of Firestore reads per search)."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0
        self.counting = False
        for name in _READS:
            original = getattr(self, name)

            async def counted(*args: Any, _original: Any = original, **kwargs: Any) -> Any:
                result = await _original(*args, **kwargs)
                if self.counting:
                    self.reads += len(result) if hasattr(result, "__len__") else 1
                return result
            setattr(self, name, counted)


def _percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))] if ordered else 0.0


async def _query(search: TierCapture, repo: CountingRepo, query: str) -> Dict[str, Any]:
    search.reset()
    repo.reads, repo.counting = 0, True
    started = time.perf_counter()
    result = await search.search(EVAL_USER, DriveSearchRequest(query=query))
    ms = (time.perf_counter() - started) * 1000
    repo.counting = False
    tiers = search.captured.get("tiers") or {}
    matched = {i for items in tiers.values() for i in items}
    return {"query": query, "ms": ms, "reads": repo.reads, "matched": matched,
            "meaning": dict(search.captured.get("meaning") or {}), "pools": search.captured.get("pools"),
            "common": list(result.tiered.common) if result.tiered else [],
            "absent": list(result.tiered.absent) if result.tiered else []}


def calibrate(negatives: Sequence[Dict[str, Any]], near: Sequence[Tuple[Dict[str, Any], str]],
              topic_of: Dict[str, str]) -> Dict[str, Any]:
    """Steps 2-3 of the procedure (module docstring)."""
    rows = []
    for t in GRID:
        neg = [min(RELATED_MAX, sum(1 for i, z in r["meaning"].items() if z >= t and i not in r["matched"]))
               for r in negatives]
        hits = 0
        for r, topic in near:
            top = sorted(((z, i) for i, z in r["meaning"].items() if z >= t and i not in r["matched"]),
                         reverse=True)[:RELATED_MAX]
            hits += any(topic_of.get(i) == topic for _, i in top)
        rows.append({"t": t, "related_per_negative": statistics.mean(neg) if neg else 0.0,
                     "near_recall": hits / len(near) if near else 0.0})
    floor = next((r for r in rows if r["related_per_negative"] <= NEG_RELATED_MAX), None)
    current = min(rows, key=lambda r: abs(r["t"] - Z_RELATED))
    return {"grid": rows, "floor": floor["t"] if floor else None,
            "near_recall_at_floor": floor["near_recall"] if floor else None,
            "accepted": bool(floor and floor["near_recall"] >= NEAR_RECALL_MIN),
            "at_current_z_related": current}


async def run_size(n: int, seed: int = 7) -> Dict[str, Any]:
    corpus: Corpus = make_corpus(n, seed)
    drive = FixtureDrive(corpus.manifest, base_dir=FIXTURES)
    for path, body in corpus.texts.items():
        drive.set_content(path, body.encode("utf-8"))
    embeddings = AnchoredLexicalEmbeddings({})
    ports = SyntheticPorts(corpus, drive.path_of, on_item=embeddings.add_evidence).ports()
    repo = CountingRepo()
    clock = EvalClock()
    started = time.perf_counter()
    indexed = await index_fixture(drive, repo, embeddings, lambda lat, lon: None, EMBEDDING_SPACE, clock,
                                  throttle_pause_s=0, extractors={PARSER: TextParser()}, triage=ports["triage"],
                                  profiler=ports["profiler"], passages=ports["passages"], max_ticks=5000)
    index_s = time.perf_counter() - started
    search = TierCapture(repo, embeddings, drive, EvalDriveConnections(eval_connection(EVAL_USER)), clock=clock,
                         embedding_space=EMBEDDING_SPACE)
    item_topic = {item: corpus.truth.get(drive.path_of(item) or "", {}).get("topic", "") for item in
                  [k[1] for k in repo.records]}
    negatives = [await _query(search, repo, q) for q in corpus.negatives]
    near = [(await _query(search, repo, q), topic) for q, topic in corpus.near_misses]
    positives = [await _query(search, repo, q) for q in [t.split()[0] for t in TOPICS][:12]]
    every = negatives + [r for r, _ in near] + positives
    carded = indexed.records.get("carded", 0)
    pools = [r["pools"] for r in every if r["pools"]]
    abstain = {name: sum(1 for p in pools if p[k] == "abstain") / len(pools) if pools else None
               for k, name in enumerate(("card", "chunk", "hint"))}
    lost_to_common = [r["query"] for r in negatives if r["matched"] and r["common"]]
    return {"files": n, "carded": carded, "band": size_band(carded), "records": indexed.records,
            "ticks": indexed.ticks, "index_seconds": round(index_s, 1),
            "chunks": sum(1 for _ in repo.chunks) if hasattr(repo, "chunks") else None,
            "search_ms_p50": round(_percentile([r["ms"] for r in every], 0.5), 1),
            "search_ms_p95": round(_percentile([r["ms"] for r in every], 0.95), 1),
            "reads_p50": _percentile([r["reads"] for r in every], 0.5),
            "reads_max": max(r["reads"] for r in every),
            "abstain_share": abstain,
            "negatives_matched": sum(1 for r in negatives if r["matched"]),
            "negatives_lost_to_common": lost_to_common,
            "positives_with_common": sum(1 for r in positives if r["common"]),
            "calibration": calibrate(negatives, near, item_topic)}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.validation.drive_eval.scale_calibrate")
    parser.add_argument("--sizes", default="200,1000,3000")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--report", type=Path, default=_REPO / "scripts" / "memory" / "drive_scale_report.json")
    args = parser.parse_args(argv)
    out = []
    for n in [int(x) for x in args.sizes.split(",") if x.strip()]:
        row = asyncio.run(run_size(n, args.seed))
        out.append(row)
        cal = row["calibration"]
        print(f"n={n} carded={row['carded']} band={row['band']} index={row['index_seconds']}s ticks={row['ticks']} "
              f"search p50/p95={row['search_ms_p50']}/{row['search_ms_p95']} ms reads p50/max={row['reads_p50']}/"
              f"{row['reads_max']} abstain={row['abstain_share']} negatives_matched={row['negatives_matched']} "
              f"lost_to_common={row['negatives_lost_to_common']} floor={cal['floor']} "
              f"near_recall@floor={cal['near_recall_at_floor']} accepted={cal['accepted']} "
              f"@Z_RELATED={cal['at_current_z_related']}", flush=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(out, indent=1, default=str) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
