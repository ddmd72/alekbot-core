<!-- living-doc
covers:
  src/domain/rrf.py
  src/domain/deduplication_service.py
-->

# Multi-Vector RRF Search and Deduplication

Why memory search fuses ranks, not scores, from several vector queries, and how near-duplicates are judged. The
engines that use these two pieces are described in
[search enrichment](../05_building_blocks/search_enrichment/README.md) and the
[memory map](../05_building_blocks/memory/README.md); this page holds the reasoning behind them.

## Why ranks, not scores

A cosine similarity depends on the query: 0.8 for one query embedding and 0.8 for another do not mean the same
thing, so scores from several queries cannot be merged by max, sum or weighted average. A max lets one lucky list
win, a sum is unbounded and still query-dependent, and weights would need tuning per query type. A rank is comparable
everywhere: rank 1 always means "best in this list".

Reciprocal Rank Fusion scores an item as the sum over the lists it appears in of `1 / (k + rank)`
(`src/domain/rrf.py::apply_rrf_ranking`, smoothing parameter `k`). Two consequences are the point of the design:

- **Consensus wins.** An item at ranks 2, 3 and 2 in three lists beats an item at rank 1 in one list and rank 15 in
  another: an item several representations agree on is more likely what the user meant.
- **Top ranks matter, but gently.** With the default `k` the gap between rank 1 and rank 10 is small, so appearing in
  more lists outweighs one better position.

The function is generic: `key_fn` gives an item's identity, so facts (`fact_id`) and companion records (`id`) share
one implementation.

### Traps in `apply_rrf_ranking`

- **The first appearance supplies the returned object.** When an item is in several lists, the object from the
  earliest list in `query_results` is returned; its similarity and other per-list fields come from that list only.
  Order the lists accordingly if those fields matter.
- **Duplicates inside one list count twice.** Every appearance adds to the score, so a list must be unique by key.
- **Ties keep first-seen order** (the sort is stable over insertion order).
- **A failed channel disappears for the caller.** `SearchEnrichmentService._search_by_vector_field` catches the
  failure, logs a warning and returns an empty list (`_safe_results` is a backstop), so the fusion proceeds on the
  remaining channels without any error to the caller.

## Why several vectors

The fields the RRF channels search are a fact's text (`vector`), its tags (`tags_vector`) and its structured data
(`metadata_vector`); all are on `src/domain/entities.py::FactEntity`, which also carries `label_vector` for the scored
fact search. One query embedding against one field loses information:
a domain noun matches tags better than prose, a narrative phrase matches the text, a date or an identifier matches
metadata. `enrich_context` takes keywords and two search phrases; each is embedded once and sent to the fields that suit its
kind, and RRF merges the lists. Its only caller is the consolidator's fact search, where phrase 2 falls back to the
primary query.

`SearchEnrichmentService.enrich_context` (`src/services/search_enrichment_service.py`) wires this as up to six
parallel channels, each a vector search over one field:

| Query | Fields | Reasoning |
|---|---|---|
| keywords | tags, metadata | domain nouns are compressed knowledge; tags hit them directly |
| phrase 1 | text, tags | a sentence carries narrative and context |
| phrase 2 | text, metadata | the alternative phrasing; structure may matter |

Per-channel limits come from `src/domain/search.py::SearchLimits`; one channel of each query gets a reduced share
of that query's limit, the other the full limit. An empty query slot adds no channels, and the texts are embedded in
one batch call.

**Where it fits and where it does not.** Several channels cost several vector reads per search and add latency, which
is acceptable for a personal memory where precision matters more than query volume. They add nothing when queries
are single keywords or when the data has no tags or metadata worth separating.

## Where each engine stands

- `SearchEnrichmentService` (fused channels, RRF, then semantic deduplication) is now called only by the
  consolidation fact-management adapter (`src/adapters/fact_management_adapter.py`), which turns the semantic
  deduplication off.
- The companion context assembler uses `apply_rrf_ranking` directly over its own per-phrase lists
  (`src/services/companion_context_assembler_service.py`), not the enrichment service.
- The Router prefetch, `search_memory`, the voice companion and the claude.ai connector use the scored fact search
  (`src/services/fact_search_service.py::FactSearchService`, `docs/10_rfcs/FACT_PROFILE_RFC.md` §5.3): candidate
  lists only generate candidates, a score orders them, and several phrases are fused by priority
  (`fuse_phrases`) instead of by RRF. The reasoning above still applies to how candidates are generated.
- The result cap resolves user, then account, then system
  (`src/services/configuration_service.py::ConfigurationService.get_semantic_search_limit`,
  `src/domain/settings.py::SearchConfig.DEFAULT_SEMANTIC_SEARCH_LIMIT`).

## Deduplication: number-aware, detail-preserving

Similar is not the same: two facts can embed almost identically and still say different things. The rule is
**better to keep a near-duplicate than to lose information.** `SmartDeduplication.is_duplicate`
(`src/domain/deduplication_service.py`, pure logic) takes the new text, the existing text and their cosine
similarity, and decides in this order:

1. similarity below `moderate_threshold` is not a duplicate;
2. if either text has numbers and the sorted number lists differ, not a duplicate;
3. similarity at or above `strict_threshold` is a duplicate;
4. if the existing text is shorter than `LENGTH_RATIO_THRESHOLD` of the new one, the new fact carries more
   detail: not a duplicate;
5. otherwise a duplicate.

**Numbers outrank similarity.** Two facts that differ in one figure can embed almost identically, so a plain threshold
would merge them; the number check keeps both. The numbers are compared sorted, so order does not matter; a date
counts as its parts and a range as its two ends. A sign or a unit is not read.

**Length keeps detail.** At a moderate similarity a clearly longer new fact is kept even when the numbers match.

**Argument order matters.** Rule 4 is asymmetric: the first text is the candidate, the second the one already held.
A shorter candidate against a longer kept fact is a duplicate; the reverse is not.

### Where deduplication applies

| Path | Dedup | Why |
|---|---|---|
| Search enrichment results (`SearchEnrichmentService._deduplicate_semantic`, `dedup_threshold`) | on when `skip_semantic_dedup` is false; `strict_threshold` = the parameter; no production caller uses it | a reader would get clean results |
| Scored fact search (`FactSearchService._is_duplicate`) | on, default thresholds | the same rule over the kept list |
| Consolidation search (`skip_semantic_dedup=True`) | off | the agent must see every candidate to decide a MERGE |
| Consolidation writes (`FactWriteService.build_facts`) | none | the model decides duplicates after searching |
| `FirestoreFactRepository.add_fact_if_unique` | on, defaults | no production caller in `src/`; consolidation does not use it |

**Why consolidation turns it off:** the consolidator searches before it writes and needs the scattered, overlapping
facts side by side to merge them. A filtered result would hide exactly the facts it exists to merge. RRF already
collapses the same fact id, which is enough there.

### Traps in the search-path dedup

- It is greedy and order-dependent: each fact is compared only with the facts already kept, so the higher-ranked of
  two near-duplicates survives and the later one is the candidate. The order is RRF order in the enrichment path and
  the `fuse_phrases` priority order in the scored search (`keep_distinct`).
- A fact without a vector is dropped from the result in the enrichment path (it cannot be compared); in the scored
  search it is simply never judged a duplicate.
- The comparison is quadratic in the kept list; the limits keep it small.

## Further reading

- Reciprocal Rank Fusion: Cormack, Clarke and Büttcher, SIGIR 2009.
- [Search enrichment](../05_building_blocks/search_enrichment/README.md), [memory](../05_building_blocks/memory/README.md).
