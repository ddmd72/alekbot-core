# Decision: The drive parser's child process bounds resources and drops privileges; it does not contain code execution

**Date:** 2026-10-13 (premise and hardening revised after the final security review, R3 F3-1)
**Status:** Accepted, with an owner-accepted residual risk and a named trigger (User Drive Step 2, Slice 2+4;
adversarial review A, final review 3). Residual risk accepted by the owner on 2026-10-13 for the current whitelist;
revisit before the whitelist admits anyone beyond the people the owner vouches for (see Trigger).
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §9, §16 (Phase 0), §24.3,
§24.14; `src/adapters/subprocess_content_extractor.py`, `src/adapters/drive_parser/child.py`,
`src/adapters/drive_parser/readers.py`, `src/utils/document_hardening.py`, `Dockerfile`, `indexer_main.py`.

## Problem

The indexer parses every admitted file on the user's drive: PDF, DOCX, PPTX, XLSX/XLS, ODF, EPUB, HTML, RTF, MSG, EML
and text. The parsers (pdfminer.six, pypdf, markitdown's converters over mammoth, python-pptx and lxml, openpyxl,
xlrd) are large native-and-Python stacks written for trusted input. Phase 0 measured what hostile or merely odd files
do to them: a 5 KB XLSX peaked at about 1.75 GB of memory; a deeply nested HTML hits the recursion limit; a PDF can
declare any page count. On a 2 GiB, concurrency-1 Cloud Run instance, one such file in the indexer process would kill
the tick for every user queued behind it, repeatedly, since the file stays due.

**The deployment is multi-user.** One whitelist admits several users, and one indexer parses all of their files in
the container that holds every user's index and the service's API keys. A file one user uploads must not be able to
reach another user's data or the service's credentials through the parser. (The first version of this record said
"single-owner"; that premise was false and is withdrawn.)

## Decision

Every file is read by a **fresh child Python process** (`python -B -E -s -m src.adapters.drive_parser.child`), one at a
time, and the indexer process never parses a byte:
- limits applied in the child **before any parser library is imported**: `RLIMIT_AS` 1 GiB, `RLIMIT_CPU` 90 s (soft;
  SIGXCPU first, SIGKILL 2 s later), `RLIMIT_FSIZE` 64 MiB, `RLIMIT_CORE` 0, one thread per numeric library;
- a 120 s wall-clock kill of the child's whole process group from the parent;
- an allowlisted environment (`PATH`, `HOME`, `LANG`, `LC_ALL` and thread settings), so no credential is in the
  child's own environment;
- **an unprivileged user.** The Dockerfile creates the system user `driveparser` and sets
  `DRIVE_PARSER_USER=driveparser`; `indexer_main.py` passes it to `SubprocessContentExtractor(sandbox_user=…)`. The
  child runs with that uid and gid and no supplementary groups. A configured user that is missing, is root, or that
  the process cannot switch to fails the indexer's start — never a silent fallback. Unset (local development), the
  child runs as the developer's own user and the constructor logs a WARNING;
- a per-run directory owned by the child (its `TMPDIR` and `HOME`), removed afterwards, holding a read-only link of
  the source file; the child writes its result and any PDF window there, and the parent collects only a regular file,
  opened without following a link or blocking on a FIFO. The result is a JSON file, never stdout, so a parser that
  prints cannot corrupt it and a killed child leaves no partial result;
- the image build fails on any world-writable file or directory under `/app` (`node_modules` aside, which the indexer
  never loads) or in site-packages, so nothing the indexer loads is writable by the parser user;
- a kill is an **outcome**, not a crash: `timeout` (wall clock, SIGXCPU) or `memory` (MemoryError, a kernel SIGKILL),
  each a counted per-file retry and never routed to the model reader;
- a failure is reported as its type, code position and a hash (`failure_label`), and a failed child's stderr as its
  length and a hash — never the text, because a parser's message or traceback can quote the document;
- the same child counts a routed PDF's pages and cuts its windows for the reader, so the indexer never opens a PDF.

**What that blocks.** A process of another uid cannot read the indexer's `/proc/<pid>/environ` (the API keys, the
Microsoft client secret), cannot rewrite the app source or site-packages the indexer loads, and cannot enter another
file's temp directory (root-only).

**What it does not block — the residual risk, accepted by the owner.** The child shares the container's network
namespace. Code an exploited parser runs can still ask the metadata server for the service account's token, and
with it reach whatever that account can reach, and can reach the network. A process it starts in a new session
(`setsid`) is outside the process group the parent kills, so it can outlive the child until the instance is
recycled. The sandbox bounds **resources** and drops **privileges**; it does not **contain code execution**. The
module docstring and RFC §24.14 say so in those words.

## Why accept the residual

- The realistic threat is a malformed or amplifying file, which the limits handle, and a parser that leaks document
  text into logs, which the redaction handles. A crafted exploit needs a code-execution bug in a pinned library.
- Real containment means a separate service with its own low-privilege service account and no access to secrets
  (or gVisor-style isolation without metadata access): a fourth deploy unit, a network hop per file, and its own
  failure modes. The cheap step that removes the most direct paths (the environment and the source) is taken now;
  the expensive one waits for its trigger.
- Every parser library on this path is pinned in `requirements.txt`, so the attack surface changes only by a
  deliberate bump, which is also a `SubprocessContentExtractor.VERSION` bump.

## Rejected alternatives

- *Parse in-process with timeouts (`asyncio.wait_for`, threads):* a thread cannot be killed, and memory amplification
  takes the whole instance down before any timeout fires.
- *Keep the child as the indexer's own user (the first version):* it could read the parent's environment through
  `/proc/<ppid>/environ` — every API key on the indexer — and rewrite the code the indexer loads next.
- *A separate parser service now:* the right answer for users who do not trust one another; kept as the §9
  "separate service" adapter behind the same `ContentExtractorPort` and deferred to its trigger.
- *Claim it as a sandbox without the caveat:* a future deployment would inherit a security property it does not
  have.

## Trigger to revisit

Either of:
- a code-execution CVE in a library the child loads (markitdown and its converters, pdfminer.six / pdfplumber, pypdf,
  openpyxl, xlrd, python-pptx, mammoth, lxml);
- users who do not trust one another (the whitelist grows beyond people the owner vouches for), or files nobody on
  the whitelist chose to put there (shared folders, email attachments at scale).

The answer then is a separate parser service with its own low-privilege service account and no access to secrets,
behind the same port. The first image build must confirm that `useradd` creates `driveparser` in the base image and
that the world-writable check passes (neither was built locally). The limits stay placeholders until spike S1
measures the Phase 0 bomb set on the indexer, as the `driveparser` user
(`scripts/infrastructure/drive_extract_s1.py`).

## Process limit and log content (hardening)

The document child gets `RLIMIT_NPROC` 256 (`EXTRACT_NPROC_LIMIT`), the media child's value, when it runs as the
unprivileged user (root ignores the limit; a dev user's own processes would count against it). The document child
spawns no process and pins its numeric libraries to one thread, so the bound only stops a fork bomb.

Logs carry no content of the drive: the tick's catch-all logs the exception class, a message digest and the frames
(`exc_brief`), not the message; a failed Graph call logs the status and the enum-like `error` / `innerError` codes,
not the response body (a body or a message can quote a file name or transcript text).
