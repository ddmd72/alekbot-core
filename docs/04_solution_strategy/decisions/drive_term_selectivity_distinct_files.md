# Decision: An exact search term is judged selective by the distinct files it spans, not by how many chunks hold it

**Date:** 2026-10-13
**Status:** Accepted (User Drive Step 2, Slice 2+4; review D-M2, arbiter R1-2 and R2 S-M1)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §24.6;
`selective_term_hits`, `term_file_probes`, `term_selective` in `src/domain/drive_content_search.py`;
`DriveSearchService._term_hits` in `src/services/drive_search_service.py`; `find_chunks_by_term`,
`find_next_file_with_term` on `DriveIndexRepository`; `TERM_NONSELECTIVE_FILES` in
`src/domain/drive_index_params.py`.

## Problem

The content exact-term stream answers "find invoice INV-2024-417", "the 1 250,00 payment", "my IBAN" by matching a
query term against each chunk's `content_terms`, one Firestore `array-contains` query per term. Firestore has no
relevance order for such a query: it returns the first `CONTENT_TERM_FETCH` (100) chunks ordered by
`(item_id, generation, index)`. When a term is common — a year, a city's postcode, a company's own VAT number printed
on every letter — the first page is an alphabetical slice by opaque item id. Ranking those files would rank them by
id; anchoring their passage windows on that term would show the common term, not the passage the user asked about;
and widening to 1 000 chunks would read more of the same slice.

So the search must decide, per term, whether its page is a ranking signal or noise.

## Decision

1. **A page that was not full is complete**: every chunk holding the term is on it. The term selects.
2. **A full page is judged by the number of distinct files on it**, not by its chunk count. A term is
   **non-selective** when its full first page spans more than `TERM_NONSELECTIVE_FILES` (8) distinct files.
3. **A full page from 8 files or fewer is probed past its last file** before it is trusted. The page is
   id-ordered, so a few long files that sort first can fill it while the term sits in many more files after them. The service asks for the next file holding the term past the last item id seen
   (`find_next_file_with_term`: the same query with a cursor on `item_id`, limit 1), one new file per probe, until a
   ninth file is seen (non-selective) or the term runs out (selective). At most 8 probes per term, each one
   single-document read.
4. **A non-selective term takes no part** in the RRF fusion, in anchoring a window, in trimming a window around a
   term, or in the widening (it is never read again in that search). The other terms of the query, the chunk kNN and
   the card and name streams still run.
5. The folder in-memory path (a subtree of at most 2 000 readable chunks) reads every chunk, so its pages are complete
   and need no probe; it applies rule 2 to the complete set.

The probe count shows on the `[DriveSearch]` line as `term_probes`.

## Why distinct files

The first version (D-M2) dropped any term whose first page was full. That dropped exactly the case the stream exists
for: an IBAN or a contract number repeated in all 120 chunks of one long contract fills the page from a single file —
and that file is the one the user asked for. Counting files separates "one document that says it everywhere" (the
answer) from "many documents that each say it" (noise). The threshold is set to the same value as `K` (8), the number
of results a search returns: a term held by no more files than one answer can show still ranks usefully. It is an
initial value (RFC §24.13), not a measured one.

## Rejected alternatives

- *Drop a term whose first page is full* (D-M2 as first built): loses concentrated terms (an IBAN in one contract),
  which are the strongest exact-match answers there are.
- *Count chunks, with a higher limit:* a long file and many short ones are indistinguishable by chunk count.
- *Read the term's whole posting list to count files:* unbounded reads per search for a common term; the probe stops
  at the ninth file.
- *`array-contains-any` over all terms:* one id-ordered page shared by every term lets a common term crowd out a rare
  one; it is the failure this stream was built to avoid (RFC Δ17 is the same lesson on names).
- *Keep non-selective terms with a low weight in RRF:* RRF has no weights here, and the page order is by id, so any
  rank it contributes is arbitrary.

## Consequences

- A query made only of common terms falls back to the chunk kNN, card and name streams for ranking; the answer is no
  worse than a search without the term stream.
- Probes add at most a few single-document reads to a search with a concentrated term; none to the common case.
- The query side sends only maximal forms (an ISO date, a canonical amount, a joined id), never a fragment like a
  year, so the most common non-selective terms never become queries in the first place (review D-M1).

## Trigger to revisit

The eval or real searches show a wanted document held only through a term spanning 9–20 files (raise the threshold),
or a full-text index with relevance order replaces `content_terms` (RFC §14 seam).
