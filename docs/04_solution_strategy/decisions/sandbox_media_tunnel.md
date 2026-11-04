# Decision: media reaches the sandbox through a reverse tunnel opened by the indexer (S5, O18)

**Date:** 2026-11-04
**Status:** deployed 2026-11-04 (PR #273). Acceptance: OneDrive passed; a long Google recording found the close-order defect below.
**Context:** [UNTRUSTED_CONTENT_SANDBOX_RFC.md](../../10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md) §4.5, §4.7, §7 S5, OQ-S11;
[GOOGLE_DRIVE_RFC.md](../../10_rfcs/GOOGLE_DRIVE_RFC.md) G2; [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §27.4.

## Decision

`media.probe` and `media.audio_windows` are full-duplex HTTP/2 stream ops on the light service (`POST /v1/media.<op>`). The indexer opens the stream and
serves byte ranges of the recording; the media child's loopback range server turns ffmpeg's reads into OPEN frames; a terminal RESULT frame decides the
outcome. The same path serves OneDrive and Google. The light service needs no egress and receives no URL, credential or whole file. A spike proved the
route on Cloud Run before the build. No `SANDBOX_MEDIA` knob; the in-process reader and its relay are deleted in the same deploy (O14 pattern).

**The caller closes first (2026-11-04, after acceptance).** On RESULT the indexer ends its request half and sends nothing more; the
route ends the response only after that end, bounded by the run's deadline. The first build ended the response while the request half was still
open. Cloud Run's front end then closes that half itself (spike S4d), and with a 2 h 27 min recording's 26 MB of windows not yet delivered it
aborted the stream with RST_STREAM INTERNAL_ERROR, twice in a row, while the sandbox and Cloud Run's request log both recorded success. The spike
had sent only small frames in that direction.

## Rejected alternatives

- **(a) Whole file in one request into the work volume:** the volume is memory, so a cap far below 8 GiB, or a re-upload every step.
- **(b) Caller sends per-window byte ranges:** works only for containers whose index sits at the head; silently drops a common class.
- **(c) One-use pull URL served by the caller, fetched through a sandbox proxy:** the sandbox would fetch from a credentialed runtime, and the indexer's deploy shape cannot serve it.
- **(e) Intermediate bucket plus a signed URL:** a new store of users' recordings at rest, a full copy per file, and the owner's standing no-signed-URL rule (O6).
- **(f) Size split, long Google recordings refused:** a permanent coverage gap; kept only as the declared fallback had the spike failed.

## Why

Every container works with seeks, nothing sensitive crosses the boundary, the in-sandbox egress proxy and its allowlist are never built, and S6's
no-egress lock-down covers the whole light service. The sandbox steers which ranges the indexer fetches, bounded by an open count, a byte budget and a ceiling.

## Triggers to revisit

The platform stops carrying plain full-duplex HTTP/2 streams on Cloud Run; the open or byte bounds prove too tight in acceptance; the light cold start
with ffmpeg proves unacceptable (split a media service); a second decoder needing seekable local files.
