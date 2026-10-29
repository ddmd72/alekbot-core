# Decision: The indexer downloads a PDF up to 100 MiB; chat/open and every other kind keep 45 MiB

**Date:** 2026-10-29
**Status:** Accepted (owner delegated the design, 2026-10-29); not deployed.
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §5.4, §15; code `size_cap` in
`src/domain/drive_index_policy.py`, `MAX_PDF_DOWNLOAD_BYTES` in `src/domain/drive_index_params.py`.

**Decision:** `size_cap("pdf")` is `MAX_PDF_DOWNLOAD_BYTES` (100 MiB). Every other content kind stays at `MAX_DOWNLOAD_BYTES`
(45 MiB), sheets at 10 MiB. `size_cap` is read only by the indexer: the planner's admission, `_fetch`'s download and the
parser's check of the sniffed kind. Chat and open use `MAX_DRIVE_DOWNLOAD_BYTES` (45 MiB, `user_drive.py`), unchanged.

**Bounds checked in code before the raise:**
- the download streams to the temp file in 64 KiB chunks and stops past the cap (`OneDriveAdapter.download_to_file`). The
  indexer process reads only the 8 KiB sniff head and hashes extracted text, never the file;
- every parse, page count and window cut runs in the child under `RLIMIT_AS` 1 GiB, `RLIMIT_FSIZE` 64 MiB and the 120 s
  wall clock. A kill is `timeout` or `memory`, retried per file, `failed` at 3, never routed to the reader;
- the reader never renders: it sends PDF windows of 10 pages that the child cut. A window is at most 64 MiB (FSIZE), and
  pypdf refuses any single stream over 75 MB. So the indexer process holds at most 4 windows (`MODEL_IN_FLIGHT`), whatever
  the source size.

**Measured** (macOS, a machine under heavy load, where `RLIMIT_AS` is not enforced). The indexer process stayed at
60-66 MB RSS in every run. A 94 MB scan-shaped PDF (94 pages): parse `no_text` in 2 s with a 363 MB child; page count
1.4 s; a 10 MB window cut in 1.5 s with a 141 MB child. A 97 MB text-only PDF (56 773 pages): parse `timeout` at 120 s
with a 406 MB child. A 47 MB text-only PDF (27 768 pages) also times out, so the timeout comes from the page count, not
the new cap. A shared 87 MB image: the cut fails `error` (pypdf limit), a per-file outcome.

**Rejected:** raising every document kind. Text kinds gain nothing, because `MAX_TEXT_CHARS` cuts their text far below
45 MiB. The ZIP kinds (docx, pptx, odt, epub) already meet the 100 MiB inflated-container check and were not measured.
A larger cap for chat and open was also rejected: 45 MiB is Telegram's limit there.

**Revisit when:** indexer memory (`alek_memory_dev`) after the failed-content requeue shows a peak near 2 GiB on a large
PDF. The worst case is 4 windows of 64 MiB plus their base64 copies, held while the child cuts the next window.
