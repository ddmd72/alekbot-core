# Decision: The machine-text gate needs repetition AND low variety, so statements, invoices and ledgers pass

**Date:** 2026-10-13
**Status:** Accepted, calibration open (User Drive Step 2, Slice 2+4; adversarial review B-M1/B-M5, reviewer B1)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §5.5 class 3, §8.1 step 6, §8.3,
§24.14; `src/domain/drive_text_gate.py` (`measure`, `is_machine_text`); `gate_exempt` in
`src/domain/drive_index_run.py`; `DriveIndexingService._read_text`.

## Problem

Machine text disguised as text — a server log, a SQL or HAR dump, telemetry saved as `.txt` — would fill the index
with near-identical chunks: hundreds of chunks that all match every kNN query about "errors" or "requests", eating the
user's 50 000-chunk budget and crowding real documents out of results. §5.5 class 3 asks for a deterministic gate that
demotes such files to `metadata_only / machine_text` (card with lead text, no chunks).

The obvious signal, line repetition, is also exactly what the documents the drive index exists for look like. A bank
statement is 1 500 lines of `date · merchant · amount`; an invoice repeats a line template; a payroll, a balance sheet
and a price list are rows of the same shape. A gate on repetition alone demotes the user's financial documents — the
ones Smart is most often asked to quote an amount from.

## Decision

`is_machine_text(text)` (tables — CSV, XLSX, XLS, ODS — never reach it, `gate_exempt`):
1. **Residue.** Each line loses a leading timestamp, IP address, WebVTT voice tag or speaker tag (`Alice:`,
   `[Bob]`); a line that is then only a timestamp or a number is ignored; Markdown table rows are not measured. Meeting
   transcripts, chat exports and dated journals keep prose after their prefixes.
2. **Bounded sample.** Metrics are taken over at most `SAMPLE_LINES` (2 000) residue lines spread evenly over the
   text, and token variety over `VARIETY_SAMPLE` (2 000) tokens spread over those lines. Tokens holding a digit fold to
   one token, so log lines that differ only in ids, counters or paths repeat.
3. **Never judged on too little:** fewer than `MIN_RESIDUE_LINES` (20) residue lines is never machine text.
4. **Machine text is** — and only is —
   - mostly near-empty lines (> 60 % with fewer than two letters: braces, separators); a line carrying an amount or a
     date is never near-empty, so a column of figures is not; **or**
   - **repetition AND low variety together**: more than half of the long residue lines repeat an earlier one, and the
     distinct/total token ratio is below `MIN_VARIETY` (0.04). Repetition alone is what a statement has too; the
     merchants, items, references and accounts keep its variety up; **or**
   - for a short text with too few tokens (< 400) to judge variety: more than half of its long **plain** lines — those
     carrying no amount or date — repeat. A ledger's template lines carry amounts and drop out; a log line stripped of
     its timestamp carries none and stays (reviewer B1: a short server log, under about 70 lines, otherwise passed).
5. **Linear time** (B-M1): lines split once, tokens whitespace-split and folded one by one, the regular expressions
   run only on a bounded head of a line or a single token; the gate, the chunker and the content terms run in
   `asyncio.to_thread`, so a 2-million-character file cannot stall the tick's event loop.

The calibration set in the unit tests (`tests/unit/domain/test_drive_text_gate*.py`): a bank statement over
Zipf-distributed merchants, a balance sheet, a same-template invoice, a short receipt and a short template ledger
pass, as do prose, meeting transcripts, VTT cues, chat exports and dated journals; long and short server and access
logs, an app log with levels, a SQL dump (with prices) and a telemetry dump fail. Linear time is tested on long CJK
tokens and degenerate text.

## Rejected alternatives

- *Repetition alone (the first build):* demotes bank statements, invoices and payrolls — the highest-value documents
  for "how much did I pay…" questions.
- *Exempt anything with amounts:* a log with response sizes or a telemetry dump full of decimals would pass.
- *A model judges it:* a model call per text document over the whole drive for a decision rules make in microseconds;
  and the model reader is reserved for files the parser cannot read.
- *No gate, rely on ranking:* the chunk budget is per user and finite; a few large logs would spend it.
- *Regex over the whole text:* backtracking patterns on a 2 M-character single line are a denial of service on the
  tick.

## Known limit and calibration

`MIN_VARIETY` was lowered from 0.12 to 0.04 so the synthetic statement passes with margin (dumps measure ≤ 0.015, the
statement ≥ 0.10, prose ~0.3). A real statement with far fewer distinct merchants, or a long ledger of one recurring
payment, may still measure low enough to be demoted. Demotion is visible: the card keeps its lead text, the record
reads `metadata_only (machine_text)`, and the search envelope counts it among the documents that could not be read.

## Trigger to revisit

A statement, invoice set or ledger is demoted `machine_text` in the live check or in a user's index (raise the bar's
evidence: lower `MIN_VARIETY` further or weight lines with amounts), or a log passes and floods results (the reverse).
A change to the gate bumps `EXTRACTION_VERSION`, so the upgrade pass re-reads every file the old rule decided —
the model-routed ones included, whose reader pages are billed again (RFC §24.14).
