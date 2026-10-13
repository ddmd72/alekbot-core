# Decision: The drive parser's child process is resource isolation, not code-execution containment

**Date:** 2026-10-13
**Status:** Accepted, with a named trigger (User Drive Step 2, Slice 2+4; adversarial review A)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §9, §16 (Phase 0), §24.3,
§24.14; `src/adapters/subprocess_content_extractor.py`, `src/adapters/drive_parser/child.py`,
`src/adapters/drive_parser/readers.py`, `src/utils/document_hardening.py`.

## Problem

The indexer parses every admitted file on the user's drive: PDF, DOCX, PPTX, XLSX/XLS, ODF, EPUB, HTML, RTF, MSG, EML
and text. The parsers (pdfminer.six, pypdf, markitdown's converters over mammoth, python-pptx and lxml, openpyxl,
xlrd) are large native-and-Python stacks written for trusted input. Phase 0 measured what hostile or merely odd files
do to them: a 5 KB XLSX peaked at about 1.75 GB of memory; a deeply nested HTML hits the recursion limit; a PDF can
declare any page count. On a 2 GiB, concurrency-1 Cloud Run instance, one such file in the indexer process would kill
the tick for every user queued behind it, repeatedly, since the file stays due.

## Decision

Every file is read by a **fresh child Python process** (`python -B -E -s -m src.adapters.drive_parser.child`), one at a
time, and the indexer process never parses a byte:
- limits applied in the child **before any parser library is imported**: `RLIMIT_AS` 1 GiB, `RLIMIT_CPU` 90 s (soft;
  SIGXCPU first, SIGKILL 2 s later), `RLIMIT_FSIZE` 64 MiB, `RLIMIT_CORE` 0, one thread per numeric library;
- a 120 s wall-clock kill of the child's whole process group from the parent;
- an allowlisted environment (`PATH`, `HOME`, `LANG`, `LC_ALL` and thread settings), so no credential is in the
  child's own environment;
- a private temp directory, removed afterwards; the result is a JSON file renamed into place, never stdout, so a parser
  that prints cannot corrupt it and a killed child leaves no partial result;
- a kill is an **outcome**, not a crash: `timeout` (wall clock, SIGXCPU) or `memory` (MemoryError, a kernel SIGKILL),
  each a counted per-file retry and never routed to the model reader;
- the same child counts a routed PDF's pages and cuts its windows for the reader, so the indexer never opens a PDF.

**What it is not.** The child runs as the same user, in the same container and network namespace as the indexer.
Code an exploited parser runs there can read the parent's environment through `/proc/<ppid>/environ` (the allowlist
keeps credentials out of the child's environment, not out of its reach), ask the metadata server for the service
account's token, and reach the network. The sandbox bounds **resources**; it does not **contain code execution**. The
module docstring and RFC §24.14 say so in those words.

## Why accept this

- The deployment is single-owner: the files parsed are the owner's own, in their own App Folder. The realistic threat
  is a malformed or amplifying file, which the limits handle, not an attacker who can plant a crafted exploit there.
- Real containment means a separate service with its own identity and no credentials (or gVisor-style isolation
  without metadata access): a fourth deploy unit, a network hop per file, and its own failure modes — weeks of work
  bought against a threat the deployment does not have yet.
- Every parser library on this path is pinned in `requirements.txt`, so the attack surface changes only by a
  deliberate bump, which is also a `SubprocessContentExtractor.VERSION` bump.

## Rejected alternatives

- *Parse in-process with timeouts (`asyncio.wait_for`, threads):* a thread cannot be killed, and memory amplification
  takes the whole instance down before any timeout fires.
- *A separate parser service now:* the right answer for untrusted input, disproportionate for one owner (see above);
  kept as the §9 "separate service" adapter behind the same `ContentExtractorPort`.
- *Claim it as a sandbox without the caveat:* a future multi-user deployment would inherit a security property it
  does not have.

## Trigger to revisit

Either of:
- a code-execution CVE in a library the child loads (markitdown and its converters, pdfminer.six / pdfplumber, pypdf,
  openpyxl, xlrd, python-pptx, mammoth, lxml);
- indexing files of users who do not trust one another (multi-tenant growth), or files the owner did not put there
  (shared folders, email attachments at scale).

The answer then is a separate parser service with its own identity and no access to secrets, behind the same port.
The limits stay placeholders until spike S1 measures the Phase 0 bomb set on the indexer
(`scripts/infrastructure/drive_extract_s1.py`).
