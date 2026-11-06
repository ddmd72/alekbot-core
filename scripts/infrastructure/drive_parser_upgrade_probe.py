"""
Read-only cost probe of the U5 parser upgrade (docs/10_rfcs/EXTERNAL_FILES_RFC.md §7 U5; plan §6 "Probe"). Owner-run,
local. It decides nothing: it prints what re-reading every `parser@3` drive record under the parser at this checkout would
change and cost, so the owner can check the number against the money rule before the upgrade pass is switched on.

Owner decisions of 2026-11-05 baked in:
  O-3 (b): an upgrade re-read never calls the paid reader. A record whose new parser text now routes to the reader (the
           old rules would have sent it) is counted KEPT-AS-PARSER with its pages and the dollars AVOIDED; those dollars
           are never part of the total.
  O-4 (allow): the profiler (a moved profile input fingerprint) and doc2query (changed chunks left without reusable
           hints) work of changed files is PRICED and added to the total, beside the embeddings.

What it does, per connection: the records with `extracted_by == "parser@3"` (all of them when at most --sample, else a
stratified sample per kind, flagged and extrapolated); each file is downloaded through the normal drive adapter, parsed
by the in-process child, chunked, and compared with the stored chunks of its readable generation. The verdict of one
record (`classify`) is pure and built only from the core: `same_read`, `changed_chunks`, `is_machine_text`, `model_route`,
`chunk_cap_refuses`, `input_fingerprint_of`, `reuse_source`. Embedding tokens of the changed texts are counted with the
Gemini `count_tokens` call for `gemini-embedding-2` (free); the price per 1M tokens is an argument (`billing.py` has none).
Profiler and doc2query dollars per call are the 30-day average of BigQuery `prompt_content`, priced by `billing.py`
(or the two override arguments); the reader's dollars per page are measured from the drive usage ledger and the reader
records of the same months (or the override, or the estimate constant).

Output: stdout gets counts and dollars only; JSON goes to `scripts/memory/u5_probe/` (gitignored). Neither carries a file
name, an item id, a ref or any text. NOTHING is written to Firestore, GCS, Cloud Tasks or a ledger; the only side effect
possible is the OAuth token refresh of the drive adapter through the normal credential store (the same write the
indexer makes). Run it from the checkout whose parser is the one to measure (the U5 branch head, PARSER_VERSION 3 still
or already 4: the probe reads records stamped `parser@3` either way).

Usage (project, credentials and collections from .env / ADC / APP_ENV):
    python scripts/infrastructure/drive_parser_upgrade_probe.py --user <user_id> [--connection <id>] \\
        --embed-usd-per-1m <price> [--sample 5000] [--out <dir>] \\
        [--profile-call-usd X] [--passage-call-usd X] [--reader-usd-per-page X]
Exit code: 0 when written, 1 when a connection could not be probed.
"""
import argparse
import asyncio
import json
import math
import os
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.domain.billing import calculate_cost  # noqa: E402
from src.domain.connection_scope import ConnectionScope  # noqa: E402
from src.domain.document_extraction import PARSER, ExtractionStatus, chain_for, model_route  # noqa: E402
from src.domain.document_kind import sniff_kind  # noqa: E402
from src.domain.drive_capture import file_capture  # noqa: E402
from src.domain.drive_chunker import chunk_text  # noqa: E402
from src.domain.drive_enrich_plan import chunks_to_hint, input_fingerprint_of  # noqa: E402
from src.domain.drive_extraction import ExtractionOutcome, ExtractionRequest  # noqa: E402
from src.domain.drive_hints import old_chunk, reuse_source  # noqa: E402
from src.domain.drive_index import (  # noqa: E402
    PASSAGES_VERSION, DriveChunk, DriveIndexRecord, Reason, RecordState, TriageVerdict,
)
from src.domain.drive_index_params import (  # noqa: E402
    PASSAGE_CHUNKS_PER_CALL, READER_PAGE_USD_ESTIMATE,
)
from src.domain.drive_index_policy import Admission, admission, size_cap  # noqa: E402
from src.domain.drive_index_run import (  # noqa: E402
    RunStep, chunk_cap_refuses, gate_exempt, outcome_step, reader_pages,
)  # noqa: E402
from src.domain.drive_reread import changed_chunks, same_read  # noqa: E402
from src.domain.drive_text_gate import is_machine_text  # noqa: E402

OLD_STAMP = "parser@3"                       # the records the upgrade pass would re-read
DEFAULT_SAMPLE = 5000                        # plan §6: all records up to this many, else a stratified sample
EMBED_MODEL = "gemini-embedding-2"
MONEY_RULE_USD = 3.0                         # memory `feedback_money_rules.md`: a paid run over this needs the owner's OK
PROFILER_AGENT = "drive_profiler"
PASSAGE_AGENT = "drive_passages"
USAGE_MONTHS = 6                             # the ledger months read for the reader's measured dollars per page
OUT_DIR = Path(__file__).resolve().parents[1] / "memory" / "u5_probe"
_PROBED_STATES = (RecordState.INDEXED, RecordState.METADATA_ONLY, RecordState.FAILED)

# The state of one record's verdict.
SAME, CHANGED, DEMOTED, STILL_DEMOTED, ERROR = "same", "changed", "demoted", "still_demoted", "error"


# -- the pure verdict of one record ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    """What re-reading one record would do. `changed_texts` (the distinct new chunk texts Layer 1 would embed) is held in
    memory only, for the token count; it never reaches the report."""
    state: str
    reason: str = ""                       # the demotion or error reason value
    kept_as_parser: bool = False           # the old rules would have sent it to the reader; the parser text is kept
    routed_pages: int = 0                  # the pages the reader would have been paid for
    changed_chunk_count: int = 0
    changed_texts: Tuple[str, ...] = ()
    profile_job: bool = False              # the profile input fingerprint moves: the profiler runs again
    hint_chunks: int = 0                   # changed chunks that take no stored hint: doc2query runs for them
    chunk_cap_refused: bool = False
    promoted: bool = False                 # it had no readable text before (demoted or failed) and now has


def classify(rec: DriveIndexRecord, stored: Sequence[DriveChunk], outcome: ExtractionOutcome, *, space: str,
             readable_total: int, hinted_indexes: FrozenSet[int] = frozenset()) -> Verdict:
    """The verdict of one record from the new parser outcome, the stored chunks of its readable generation, the tick's
    embedding space, the user's readable chunk total and the indexes of the stored chunks that carry a current hint.
    Pure; every rule is the core's (the indexer's own steps in the indexer's order)."""
    kind = rec.kind
    had_text = rec.indexed_generation is not None and rec.state is RecordState.INDEXED
    why = model_route(outcome, kind)
    kept = why is not None and outcome.status is ExtractionStatus.TEXT
    pages = reader_pages(kind, outcome) if kept else 0
    if outcome.status in (ExtractionStatus.TEXT, ExtractionStatus.NO_TEXT):
        capture, source = file_capture(
            kind, signals=outcome.capture_signals, media=rec.media, model=outcome.capture,
            triaged=rec.capture if (rec.triage_verdict is TriageVerdict.TEXT
                                    and rec.triaged_content_version == rec.content_version) else "")
        outcome = replace(outcome, capture=capture, capture_source=source)
    step, reason = outcome_step(outcome.status)
    if reason is not None:
        return _flip(rec, had_text, reason, step is RunStep.RETRY, kept, pages)
    if outcome.status is not ExtractionStatus.TEXT:
        return Verdict(ERROR, reason=outcome.status.value)
    if not gate_exempt(kind) and is_machine_text(outcome.text):
        return _flip(rec, had_text, Reason.MACHINE_TEXT, False, kept, pages)
    chunked = chunk_text(outcome.text)
    if not chunked.chunks:
        return _flip(rec, had_text, Reason.NO_TEXT, False, kept, pages)
    if chunk_cap_refuses(readable_total, rec, len(chunked.chunks)):
        flip = _flip(rec, had_text, Reason.USER_CHUNK_CAP, False, kept, pages)
        return replace(flip, chunk_cap_refused=True)
    if had_text and same_read(rec, stored, chunked, outcome, space=space):
        return Verdict(SAME, kept_as_parser=kept, routed_pages=pages)
    reusable = stored if (had_text and rec.chunk_space == space) else ()
    indexes = changed_chunks(reusable, chunked)
    texts = tuple(dict.fromkeys(chunked.chunks[i].text for i in indexes))   # Layer 1 embeds one call per distinct text
    new_chunks = [DriveChunk(user_id=rec.user_id, item_id=rec.item_id, generation=1, index=i, text=c.text,
                             char_start=c.char_start, char_end=c.char_end, kind=kind)
                  for i, c in enumerate(chunked.chunks)]
    done = replace(rec, state=RecordState.INDEXED, reason=None, chunk_count=len(new_chunks),
                   text_chars=len(outcome.text), script_rendered=outcome.script_rendered)
    fingerprint = input_fingerprint_of(done, new_chunks)
    profile_job = fingerprint is not None and fingerprint != rec.profile_input_fingerprint
    return Verdict(CHANGED, kept_as_parser=kept, routed_pages=pages, changed_chunk_count=len(indexes),
                   changed_texts=texts, profile_job=profile_job,
                   hint_chunks=_unhinted(done, new_chunks, indexes, stored, hinted_indexes),
                   promoted=not had_text)


def _flip(rec: DriveIndexRecord, had_text: bool, reason: Reason, retry: bool, kept: bool, pages: int) -> Verdict:
    """A record that now demotes: a flip when it was readable, nothing new when it already stood demoted for that
    reason; a retry step is an error of the file."""
    if retry:
        return Verdict(ERROR, reason=reason.value)
    if not had_text and rec.state is RecordState.METADATA_ONLY and rec.reason is reason:
        return Verdict(STILL_DEMOTED, reason=reason.value)
    return Verdict(DEMOTED, reason=reason.value, kept_as_parser=kept, routed_pages=pages)


def _unhinted(done: DriveIndexRecord, new_chunks: Sequence[DriveChunk], changed: Sequence[int],
              stored: Sequence[DriveChunk], hinted_indexes: FrozenSet[int]) -> int:
    """Changed chunks, among the ones that get hints, that no stored hint covers (`reuse_source`): what doc2query would
    run for. A file with no current hint at all gets none here: its hints are pending work whatever the parser is."""
    if not hinted_indexes:
        return 0
    olds = [old_chunk(c.index, c.text) for c in stored if c.index in hinted_indexes]
    limit = chunks_to_hint(done)
    return sum(1 for i in changed if i < limit and reuse_source(new_chunks[i].text, olds) is None)


# -- sampling, prices, aggregation (pure) ----------------------------------------------------------------------------

def stratified_sample(records: Sequence[DriveIndexRecord], limit: int) -> Tuple[List[DriveIndexRecord], Dict[str, Tuple[int, int]]]:
    """All records when there are at most `limit`; else about `limit` of them with every kind kept in proportion (at
    least one of each), taken evenly over the item ids sorted (deterministic). Returns (the records to probe, {kind:
    (records of the kind, records probed)})."""
    by_kind: Dict[str, List[DriveIndexRecord]] = {}
    for rec in records:
        by_kind.setdefault(rec.kind, []).append(rec)
    if len(records) <= limit:
        return list(records), {k: (len(v), len(v)) for k, v in by_kind.items()}
    chosen: List[DriveIndexRecord] = []
    counts: Dict[str, Tuple[int, int]] = {}
    for kind, items in sorted(by_kind.items()):
        items.sort(key=lambda r: r.item_id)
        take = min(len(items), max(1, round(limit * len(items) / len(records))))
        step = len(items) / take
        chosen.extend(items[int(i * step)] for i in range(take))
        counts[kind] = (len(items), take)
    return chosen, counts


def average_call_usd(rows: Iterable[Mapping[str, Any]], agent_type: str) -> Optional[float]:
    """The mean dollars of one call of `agent_type` over `prompt_content` rows grouped by (agent_type, model) with `n`
    calls, `prompt_tokens` and `completion_tokens`, priced by `billing.calculate_cost`. None when no call was seen."""
    calls, usd = 0, 0.0
    for row in rows:
        if row["agent_type"] != agent_type:
            continue
        calls += int(row["n"])
        usd += calculate_cost(str(row["model"]), int(row["prompt_tokens"] or 0), int(row["completion_tokens"] or 0))
    return usd / calls if calls else None


def reader_usd_per_page(reader_usd_by_month: Mapping[str, float],
                        reader_reads: Iterable[Tuple[Optional[datetime], int]]) -> Tuple[float, str]:
    """(dollars per page, source): the reader's ledger dollars of the months that have any, over the pages of the reader
    records last settled by the reader in those months; the estimate constant when either side is empty."""
    months = {m for m, usd in reader_usd_by_month.items() if usd > 0}
    pages = sum(p for at, p in reader_reads if at is not None and at.strftime("%Y-%m") in months)
    usd = sum(reader_usd_by_month[m] for m in months)
    if usd > 0 and pages > 0:
        return usd / pages, "measured"
    return READER_PAGE_USD_ESTIMATE, "estimate"


@dataclass
class KindTotals:
    """Counts of one kind over the probed records."""
    records: int = 0
    states: Counter = field(default_factory=Counter)
    promoted: int = 0
    changed_chunks: int = 0
    tokens: int = 0
    tokens_estimated: int = 0              # files whose tokens are a characters/4 estimate (count_tokens failed)
    profile_jobs: int = 0
    hint_chunks: int = 0
    hint_calls: int = 0
    kept_records: int = 0
    kept_pages: int = 0
    demotes: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)
    chunk_cap_refusals: int = 0

    def add(self, verdict: Verdict, tokens: int = 0, estimated: bool = False) -> None:
        self.records += 1
        self.states[verdict.state] += 1
        self.promoted += int(verdict.promoted)
        self.changed_chunks += verdict.changed_chunk_count
        self.tokens += tokens
        self.tokens_estimated += int(estimated)
        self.profile_jobs += int(verdict.profile_job)
        self.hint_chunks += verdict.hint_chunks
        self.hint_calls += math.ceil(verdict.hint_chunks / PASSAGE_CHUNKS_PER_CALL)
        if verdict.kept_as_parser:
            self.kept_records += 1
            self.kept_pages += verdict.routed_pages
        if verdict.state == DEMOTED:
            self.demotes[verdict.reason] += 1
        if verdict.state == ERROR:
            self.errors[verdict.reason] += 1
        self.chunk_cap_refusals += int(verdict.chunk_cap_refused)

    def error(self, reason: str) -> None:
        """A file that could not be read here at all (download, parser): counted, never classified."""
        self.records += 1
        self.states[ERROR] += 1
        self.errors[reason] += 1


@dataclass(frozen=True)
class Prices:
    embed_usd_per_1m: float
    profile_call_usd: Optional[float]
    passage_call_usd: Optional[float]
    reader_usd_per_page: float
    reader_source: str


def _usd(totals: KindTotals, prices: Prices, scale: float) -> Dict[str, float]:
    embed = totals.tokens / 1_000_000 * prices.embed_usd_per_1m * scale
    profile = totals.profile_jobs * (prices.profile_call_usd or 0.0) * scale
    passage = totals.hint_calls * (prices.passage_call_usd or 0.0) * scale
    return {"embeddings": round(embed, 4), "profile": round(profile, 4), "doc2query": round(passage, 4),
            "total": round(embed + profile + passage, 4),
            "reader_avoided": round(totals.kept_pages * prices.reader_usd_per_page * scale, 4)}


def kind_report(totals: KindTotals, prices: Prices, counts: Tuple[int, int]) -> Dict[str, Any]:
    """One kind: its counts over the probed records, the scale to the kind's whole (`all / probed`) and its dollars as
    probed and extrapolated. `reader_avoided` is what the old rules would have paid the reader; never in `total`."""
    scale = counts[0] / counts[1] if counts[1] else 1.0
    return {"records_of_kind": counts[0], "probed": totals.records, "scale": round(scale, 4),
            "same": totals.states[SAME], "changed": totals.states[CHANGED], "promoted": totals.promoted,
            "still_demoted": totals.states[STILL_DEMOTED], "changed_chunks": totals.changed_chunks,
            "tokens": totals.tokens, "tokens_estimated_files": totals.tokens_estimated,
            "profile_jobs": totals.profile_jobs, "hint_chunks": totals.hint_chunks, "hint_calls": totals.hint_calls,
            "kept_as_parser": {"records": totals.kept_records, "pages": totals.kept_pages},
            "demote_flips": dict(sorted(totals.demotes.items())),
            "chunk_cap_refusals": totals.chunk_cap_refusals, "errors": dict(sorted(totals.errors.items())),
            "usd": _usd(totals, prices, 1.0), "usd_extrapolated": _usd(totals, prices, scale)}


def build_report(per_kind: Mapping[str, KindTotals], counts: Mapping[str, Tuple[int, int]], prices: Prices,
                 *, sample_limit: int, connections: int, now: datetime) -> Dict[str, Any]:
    """The probe's JSON: per kind, totals (embeddings + O-4 enrichment) as probed and extrapolated, the avoided reader
    dollars apart, and the flags that say how far to trust it. No names, refs or text."""
    kinds = {kind: kind_report(t, prices, counts.get(kind, (t.records, t.records))) for kind, t in sorted(per_kind.items())}
    sampled = any(c[0] != c[1] for c in counts.values())

    def total(key: str, name: str) -> float:
        return round(sum(k[key][name] for k in kinds.values()), 4)

    return {
        "generated_at": now.isoformat(), "stamp_probed": OLD_STAMP, "connections": connections,
        "sampled": sampled, "sample_limit": sample_limit,
        "prices": {"embed_usd_per_1m_tokens": prices.embed_usd_per_1m, "profile_call_usd": prices.profile_call_usd,
                   "doc2query_call_usd": prices.passage_call_usd, "doc2query_chunks_per_call": PASSAGE_CHUNKS_PER_CALL,
                   "reader_usd_per_page": round(prices.reader_usd_per_page, 6),
                   "reader_usd_per_page_source": prices.reader_source},
        "unpriced": [n for n, v in (("profile_call_usd", prices.profile_call_usd),
                                    ("doc2query_call_usd", prices.passage_call_usd)) if v is None],
        "kinds": kinds,
        "totals": {"records": sum(k["probed"] for k in kinds.values()),
                   "usd": {n: total("usd", n) for n in ("embeddings", "profile", "doc2query", "total")},
                   "usd_extrapolated": {n: total("usd_extrapolated", n)
                                        for n in ("embeddings", "profile", "doc2query", "total")},
                   "reader_avoided_usd": total("usd", "reader_avoided"),
                   "reader_avoided_usd_extrapolated": total("usd_extrapolated", "reader_avoided")},
        "over_money_rule": total("usd_extrapolated", "total") > MONEY_RULE_USD,
    }


def summary_lines(report: Mapping[str, Any]) -> List[str]:
    """The stdout lines: counts and dollars, no names."""
    totals = report["totals"]
    lines = [f"probed {totals['records']} records ({'a stratified SAMPLE' if report['sampled'] else 'all'}) of "
             f"{report['connections']} connection(s)"]
    for kind, k in report["kinds"].items():
        lines.append(f"  {kind}: {k['probed']} probed / {k['records_of_kind']}; same {k['same']}, changed {k['changed']}, "
                     f"chunks {k['changed_chunks']}, tokens {k['tokens']}, profile jobs {k['profile_jobs']}, "
                     f"hint chunks {k['hint_chunks']}, kept-as-parser {k['kept_as_parser']['records']} "
                     f"({k['kept_as_parser']['pages']} pages, ${k['usd']['reader_avoided']} avoided), "
                     f"demote flips {k['demote_flips']}, cap refusals {k['chunk_cap_refusals']}, errors {k['errors']}")
    e = totals["usd_extrapolated"]
    lines.append(f"TOTAL (extrapolated to the whole): embeddings ${e['embeddings']} + profiler ${e['profile']} + "
                 f"doc2query ${e['doc2query']} = ${e['total']}")
    lines.append(f"  reader dollars AVOIDED (never spent, not in the total): ${totals['reader_avoided_usd_extrapolated']}")
    if report["unpriced"]:
        lines.append(f"  UNPRICED (no calls in 30 days; pass the override): {', '.join(report['unpriced'])}")
    if report["over_money_rule"]:
        lines.append(f"  OVER the ${MONEY_RULE_USD:g} money rule: the owner's OK is needed before the pass is switched on")
    return lines


# -- I/O: reads only -------------------------------------------------------------------------------------------------

TokenCounter = Callable[[Sequence[str]], Awaitable[Tuple[int, bool]]]


async def read_parser_records(repo: Any, scope: ConnectionScope) -> Tuple[List[DriveIndexRecord], List[Tuple[Optional[datetime], int]]]:
    """(the records stamped `parser@3` that the upgrade pass would re-read, the reader records' (settled-at, pages) for
    the dollars-per-page measure), by paging the whole connection."""
    due: List[DriveIndexRecord] = []
    reads: List[Tuple[Optional[datetime], int]] = []
    after: Optional[str] = None
    while True:
        page = await repo.records_page(scope, after, 200)
        for rec in page:
            if (rec.extracted_by or "").startswith("reader@"):      # None: never extracted, or reset by the planner
                reads.append((rec.model_read_at, rec.pages_total or 1))
            if (rec.extracted_by == OLD_STAMP and rec.state in _PROBED_STATES and not rec.is_folder
                    and admission(rec.kind) is Admission.CONTENT and chain_for(rec.kind)[0] == PARSER):
                due.append(rec)
        if len(page) < 200:
            return due, reads
        after = page[-1].item_id


async def probe_record(rec: DriveIndexRecord, *, repo: Any, drive: Any, handle: Any, scope: ConnectionScope,
                       extractor: Any, space: str, readable_total: int, workdir: str,
                       count: TokenCounter) -> Tuple[Verdict, int, bool]:
    """One file: downloaded, parsed (the reader is never called: `max_model_pages` 0), classified; (verdict, tokens of
    its changed texts, whether those tokens are an estimate). Raises what the download raises."""
    path = os.path.join(workdir, "probe.bin")
    await drive.download_to_file(handle, rec.item_id, path, size_cap(rec.kind))
    with open(path, "rb") as fh:
        kind = sniff_kind(fh.read(4096), rec.kind)
    if kind != rec.kind and admission(kind) is not Admission.CONTENT:
        return Verdict(DEMOTED, reason=Reason.KIND_NOT_ADMITTED.value), 0, False
    outcome = await extractor.extract(ExtractionRequest(path=path, name=rec.name, kind=kind, mime_type=rec.mime_type,
                                                        user_id=rec.user_id, account_id="", max_model_pages=0,
                                                        item_id=rec.item_id))
    stored: List[DriveChunk] = []
    hinted: FrozenSet[int] = frozenset()
    generation = rec.indexed_generation
    if generation is not None and rec.chunk_count > 0:
        stored = [c for c, _ in await repo.get_chunks_with_vectors(scope, rec.item_id, generation,
                                                                   range(rec.chunk_count), terms=False, vectors=False)]
        hinted = frozenset(h.index for h in await repo.get_hints(scope, rec.item_id, generation)
                           if h.passages_version == PASSAGES_VERSION)
    verdict = classify(rec, stored, outcome, space=space, readable_total=readable_total, hinted_indexes=hinted)
    tokens, estimated = await count(verdict.changed_texts) if verdict.changed_texts else (0, False)
    return verdict, tokens, estimated


def token_counter(genai_client: Any) -> TokenCounter:
    """The free Gemini `count_tokens` call for the embedding model, in batches of 50 texts; a failure falls back to
    characters / 4 for that batch and says so (`tokens_estimated`)."""
    async def count(texts: Sequence[str]) -> Tuple[int, bool]:
        total, estimated = 0, False
        for start in range(0, len(texts), 50):
            batch = list(texts[start:start + 50])
            try:
                resp = await genai_client.aio.models.count_tokens(model=EMBED_MODEL, contents=batch)
                total += int(resp.total_tokens)
            except Exception as exc:   # an estimate is better than no number; the report says how many files used it
                print(f"count_tokens failed ({type(exc).__name__}); estimating this batch at characters / 4")
                total += sum(len(t) for t in batch) // 4
                estimated = True
        return total, estimated
    return count


async def bigquery_rows(bq: Any, table: str) -> List[Dict[str, Any]]:
    """The 30-day calls of the profiler and doc2query agents, grouped by agent and model (one read query)."""
    sql = (f"SELECT agent_type, model, COUNT(*) AS n, SUM(prompt_tokens) AS prompt_tokens, "
           f"SUM(completion_tokens) AS completion_tokens FROM `{table}` "
           f"WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
           f"AND agent_type IN ('{PROFILER_AGENT}', '{PASSAGE_AGENT}') GROUP BY agent_type, model")
    job = await asyncio.to_thread(bq.query, sql)
    return [dict(row) for row in await asyncio.to_thread(job.result)]


async def reader_ledger(repo: Any, user_id: str, now: datetime) -> Dict[str, float]:
    """The user's reader dollars per ledger month, the last USAGE_MONTHS months."""
    out: Dict[str, float] = {}
    year, month = now.year, now.month
    for _ in range(USAGE_MONTHS):
        key = f"{year:04d}-{month:02d}"
        out[key] = float((await repo.get_model_usage(user_id, key)).reader_usd)
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return out


async def run(connections: Sequence[Tuple[Any, Any]], *, repo: Any, drive: Any, extractor: Any, space: str,
              count: TokenCounter, sample: int, log: Callable[[str], None] = print
              ) -> Tuple[Dict[str, KindTotals], Dict[str, Tuple[int, int]], List[Tuple[Optional[datetime], int]], int]:
    """The probe over (connection, user's reader ledger unused here) pairs; returns the per-kind totals, the per-kind
    (all, probed) counts, the reader reads and the number of connections that could not be probed."""
    per_kind: Dict[str, KindTotals] = {}
    counts: Dict[str, Tuple[int, int]] = {}
    reads: List[Tuple[Optional[datetime], int]] = []
    failed = 0
    for number, (conn, _) in enumerate(connections, 1):
        scope, handle = conn.scope, conn.handle
        records, conn_reads = await read_parser_records(repo, scope)
        reads.extend(conn_reads)
        chosen, kind_counts = stratified_sample(records, sample)
        for kind, (total, taken) in kind_counts.items():
            old = counts.get(kind, (0, 0))
            counts[kind] = (old[0] + total, old[1] + taken)
        log(f"connection {number}: {len(records)} parser records, probing {len(chosen)}")
        readable_total = await repo.readable_chunk_total(scope)
        with tempfile.TemporaryDirectory() as workdir:
            for done, rec in enumerate(chosen, 1):
                totals = per_kind.setdefault(rec.kind, KindTotals())
                try:
                    verdict, tokens, estimated = await probe_record(
                        rec, repo=repo, drive=drive, handle=handle, scope=scope, extractor=extractor, space=space,
                        readable_total=readable_total, workdir=workdir, count=count)
                except Exception as exc:   # one file's failure is counted by its type; a systemic one stops the connection
                    if type(exc).__name__ in ("DriveNotConnectedError", "DriveThrottled"):
                        log(f"connection {number} stopped: {type(exc).__name__}")
                        failed += 1
                        break
                    totals.error(type(exc).__name__)
                    continue
                totals.add(verdict, tokens, estimated)
                if done % 100 == 0:
                    log(f"  {done}/{len(chosen)}")
    return per_kind, counts, reads, failed


async def _main(args: argparse.Namespace) -> int:
    from google.cloud import bigquery

    from scripts._firestore import async_client
    from scripts.infrastructure.drive_connection_arg import resolve_connection
    from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
    from src.adapters.firestore_account_repo import FirestoreAccountRepository
    from src.composition.service_container import ServiceContainer
    from src.config.settings import load_settings

    settings = load_settings()
    db = async_client()
    env_config = settings["ENVIRONMENT_CONFIG"]
    account_repo = FirestoreAccountRepository(db_client=db, collection_name=env_config.account_collection_name)
    container = ServiceContainer(config=settings, db_client=db, env_config=env_config, account_repo=account_repo)
    repo, drive, conn_repo = container.drive_index_repo, container.user_drive, container.drive_connection_repo
    if repo is None or drive is None or conn_repo is None:
        print("the drive stack is not configured here (OAuth client keys missing)")
        return 1
    now = datetime.now(timezone.utc)
    pairs: List[Tuple[Any, Any]] = []
    for user_id in args.user:
        conn, why = await resolve_connection(conn_repo, user_id, args.connection)
        if conn is None:
            print(f"user skipped: {why}")
            continue
        pairs.append((conn, user_id))
    if not pairs:
        return 1
    space = container.embedding_service.EMBEDDING_SPACE
    count = token_counter(container.embedding_service.client)
    extractor = SubprocessContentExtractor()
    per_kind, counts, reads, failed = await run(pairs, repo=repo, drive=drive, extractor=extractor, space=space,
                                                count=count, sample=args.sample)
    profile_usd, passage_usd = args.profile_call_usd, args.passage_call_usd
    if profile_usd is None or passage_usd is None:
        table = f"{settings['GOOGLE_CLOUD_PROJECT']}.{settings['BIGQUERY_PROMPT_DATASET']}.{settings['BIGQUERY_PROMPT_TABLE']}"
        rows = await bigquery_rows(bigquery.Client(project=settings["GOOGLE_CLOUD_PROJECT"]), table)
        profile_usd = profile_usd if profile_usd is not None else average_call_usd(rows, PROFILER_AGENT)
        passage_usd = passage_usd if passage_usd is not None else average_call_usd(rows, PASSAGE_AGENT)
    if args.reader_usd_per_page is not None:
        per_page, source = args.reader_usd_per_page, "argument"
    else:
        ledger: Dict[str, float] = {}
        for _, user_id in pairs:
            for month, usd in (await reader_ledger(repo, user_id, now)).items():
                ledger[month] = ledger.get(month, 0.0) + usd
        per_page, source = reader_usd_per_page(ledger, reads)
    prices = Prices(args.embed_usd_per_1m, profile_usd, passage_usd, per_page, source)
    report = build_report(per_kind, counts, prices, sample_limit=args.sample, connections=len(pairs), now=now)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"drive_parser_upgrade_probe_{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    for line in summary_lines(report):
        print(line)
    print(f"wrote {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--user", action="append", default=[], required=True, help="a user to probe (repeatable)")
    parser.add_argument("--connection", default=None, help="the connection id (else the user's one live connection)")
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE,
                        help=f"probe all parser@3 records up to this many, else a stratified sample (default {DEFAULT_SAMPLE})")
    parser.add_argument("--embed-usd-per-1m", type=float, required=True,
                        help="gemini-embedding-2 dollars per 1M input tokens, from the provider's page that day")
    parser.add_argument("--profile-call-usd", type=float, default=None, help="override of the profiler's 30-day $/call")
    parser.add_argument("--passage-call-usd", type=float, default=None, help="override of doc2query's 30-day $/call")
    parser.add_argument("--reader-usd-per-page", type=float, default=None, help="override of the measured reader $/page")
    parser.add_argument("--out", default=str(OUT_DIR), help="the output directory (default scripts/memory/u5_probe/)")
    parsed = parser.parse_args()
    if parsed.connection and len(parsed.user) != 1:
        parser.error("--connection names one connection: use it with exactly one --user")
    sys.exit(asyncio.run(_main(parsed)))
